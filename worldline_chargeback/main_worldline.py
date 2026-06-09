"""
Worldline chargeback automation orchestrator.

For each unhandled Worldline thread:
    1. Subject parse  -> merchant_name + case_id
    2. Body table parse -> payment_id, bank_rrn, transaction_date, amount, ...
    3. Recipient list -> from original mail's To header, drop @gokwik.co +
       worldline + tpsl + easebuzz domains; remaining addresses go on To.
    4. Planhat -> CSM emails (owner + coOwner) keyed by merchant_name.
       Falls through to "no CSM" silently if not found.
    5. Compose + send reply (templates/reply_worldline.html.j2) in the same
       thread; Cc internal team + Worldline channel + CSM.
    6. Append log row, apply auto-replied-worldline label.

If recipient resolution fails (no merchant TO can be derived from the To
header), the thread is logged to the manual-review tracker and labeled
auto-logged-manual-worldline instead.

After the auto-reply pass, the shared 48h reminder pass runs.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from email.utils import getaddresses
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

import gmail_client as gm
import sheets_client as sc
import reminders
import worldline_body as wb
from logging_setup import log, setup_logging
from planhat_client import PlanhatClient

HERE = Path(__file__).parent


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f)


def split_addrs(raw: str) -> list[str]:
    return [a for _n, a in getaddresses([raw or ""]) if a]


def dedupe(addrs: list[str], drop: set[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    drop_l = {a.lower() for a in drop}
    for a in addrs:
        k = a.lower()
        if k in drop_l or k in seen:
            continue
        seen.add(k)
        out.append(a)
    return out


def render(template_rel: str, ctx: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(str(HERE)),
        autoescape=select_autoescape(["html"]),
    )
    return env.get_template(template_rel).render(ctx)


def _domain(addr: str) -> str:
    return addr.split("@", 1)[1].lower() if "@" in addr else ""


def _domain_match(addr_domain: str, internal_domains: set[str]) -> bool:
    """True when `addr_domain` is exactly an internal domain OR a subdomain
    of one. Handles e.g. `mail.in.worldline-solutions.com` matching
    `worldline-solutions.com`."""
    for d in internal_domains:
        if addr_domain == d or addr_domain.endswith("." + d):
            return True
    return False


def derive_recipients(
    thread_first_msg: dict,
    cfg: dict,
    csm_emails: list[str],
) -> tuple[list[str], list[str]]:
    """Build (To, Cc) for our outgoing reply, matching the established team
    pattern (e.g., Taufiq's manual reply to the Rezoni thread):

        To = merchant addresses from the ORIGINAL mail's To header that are
             NOT in our internal_domains (so neither gokwik / worldline /
             tpsl etc. show up as the primary recipient).
        Cc = preserve the original mail's Cc entirely (so the TPSL/Worldline
             escalation chain stays in the loop) + add internal_always_cc +
             add the merchant's CSM from Planhat. Deduped, with self and
             everything already on To excluded.
    """
    raw_to = split_addrs(gm.header(thread_first_msg, "To"))
    raw_cc = split_addrs(gm.header(thread_first_msg, "Cc"))
    self_email = cfg["self_email"].lower()
    internal_domains = {d.lower().lstrip(".") for d in cfg.get("internal_domains", [])}

    # ---- To: merchant-side addresses from original To ----
    merchant_to: list[str] = []
    seen_to: set[str] = set()
    for addr in raw_to:
        al = addr.lower()
        if al == self_email or al in seen_to:
            continue
        if _domain_match(_domain(al), internal_domains):
            continue
        seen_to.add(al)
        merchant_to.append(addr)

    # ---- Cc: preserve original Cc, then append always_cc + CSM ----
    drop = {self_email} | seen_to
    cc: list[str] = []
    seen_cc: set[str] = set()
    for source in (raw_cc, cfg.get("internal_always_cc") or [], csm_emails):
        for addr in source:
            al = addr.lower()
            if al in drop or al in seen_cc:
                continue
            seen_cc.add(al)
            cc.append(addr)

    return merchant_to, cc


# ---------------------------------------------------------------------------
# Tracker row builder
# ---------------------------------------------------------------------------

def _build_log_row(cfg: dict, fields: dict, *, columns_key: str = "log_sheet") -> list[str]:
    import datetime
    today = datetime.datetime.now().strftime("%-d-%b-%Y")
    out: list[str] = []
    for spec in cfg[columns_key].get("columns", []):
        if spec == "__today__":
            out.append(today)
        elif spec.startswith("__static__:"):
            out.append(spec.split(":", 1)[1])
        else:
            out.append(str(fields.get(spec) or ""))
    return out


# ---------------------------------------------------------------------------
# Per-thread processor
# ---------------------------------------------------------------------------

def process_thread(
    client,
    tid: str,
    cfg: dict,
    label_replied: str | None,
    label_manual: str | None,
    *,
    planhat: PlanhatClient | None,
    dry_run: bool,
) -> None:
    thread = client.get_thread(tid)
    msgs = thread.get("messages", [])
    if not msgs:
        return
    first = msgs[0]
    subject = gm.header(first, "Subject")
    print(f"[thread] {tid} subj={subject!r} msgs={len(msgs)}")

    # Gate 1: subject regex
    pat = cfg.get("subject_regex")
    if pat and not re.search(pat, subject, re.IGNORECASE):
        print("  [skip] subject regex did not match")
        return

    # Gate 2: thread must still be fresh (no human/team reply yet)
    if len(msgs) > 1:
        print(f"  [skip] thread already has {len(msgs)} messages")
        return

    # Gate 3: From must be one of the allowed senders
    sender = (gm.header(first, "From") or "").lower()
    allowed = [s.lower() for s in cfg.get("allowed_senders", [])]
    if allowed and not any(s in sender for s in allowed):
        print(f"  [skip] From={sender!r} not in allowed_senders")
        return

    # Parse subject + body
    sub = wb.parse_subject(subject)
    if not sub.get("merchant_name"):
        print("  [skip] couldn't parse merchant name from subject")
        return
    body_fields = wb.parse_fields(gm.message_html_body(first))
    if not body_fields.get("payment_id"):
        print("  [skip] body table missing SM_Transaction_ID (payment id)")
        return

    fields = {**body_fields, **sub}
    fields["merchant_name"] = sub["merchant_name"]
    print(f"  merchant={fields['merchant_name']!r}  payment_id={fields['payment_id']!r}  case={fields['case_id']!r}")

    # CSM via Planhat — best-effort, used during Cc assembly.
    csm_emails: list[str] = []
    if planhat:
        try:
            csm_emails = planhat.get_csm_emails(fields["merchant_name"]) or []
        except Exception:
            log.exception("planhat lookup failed for %r", fields["merchant_name"])
    if csm_emails:
        print(f"  csm: {csm_emails}")
    else:
        print(f"  csm: (none — replying without CSM on Cc)")

    # Recipients: To from original mail's To (non-internal), Cc preserves
    # the original Cc list + adds internal_always_cc + CSM.
    merchant_tos, cc_list = derive_recipients(first, cfg, csm_emails)
    if not merchant_tos:
        print(f"  [manual] no merchant TO could be derived for {fields['merchant_name']!r}")
        if dry_run:
            return
        try:
            row = _build_log_row(cfg, fields, columns_key="failed_log_sheet")
            resp = sc.append_log_row(client.creds, cfg["failed_log_sheet"], row)
            print(f"    [manual-log] {resp.get('updatedRange')}")
        except Exception:
            log.exception("manual log failed")
        if label_manual:
            client.add_label_to_thread(tid, label_manual)
        return

    fields["merchant_email_id"] = merchant_tos[0]
    print(f"  merchant TO: {merchant_tos}")

    subject_tpl = cfg["reply"].get("subject_override") or subject
    if not re.match(r"^\s*re:", subject_tpl, re.IGNORECASE):
        subject_tpl = f"Re: {subject_tpl}"

    last_msgid = gm.header(msgs[-1], "Message-ID")
    template_rel = cfg["reply"]["template_path"]

    ctx = {
        **fields,
        "self_email":       cfg["self_email"],
        "contact_phone":    cfg.get("contact_phone", ""),
        "contact_email":    cfg.get("contact_email", ""),
    }
    missing = [k for k in cfg["reply"].get("required_fields", []) if not ctx.get(k)]
    if missing:
        print(f"  [skip] missing required field(s) {missing}")
        return

    html = render(template_rel, ctx)
    to_hdr = ", ".join(merchant_tos)
    cc_hdr = ", ".join(cc_list)

    if dry_run:
        print(f"  [DRY-RUN]")
        print(f"    To:  {to_hdr}")
        print(f"    Cc:  {cc_hdr}")
        print(f"    Sub: {subject_tpl}")
        return

    client.send_reply(
        to=to_hdr,
        cc=cc_hdr,
        subject=subject_tpl,
        html_body=html,
        thread_id=tid if cfg["reply"].get("reply_in_thread", True) else None,
        in_reply_to=last_msgid or None,
    )
    print(f"  [sent] to={to_hdr}")

    # tracker append
    log_cfg = cfg.get("log_sheet") or {}
    if log_cfg.get("spreadsheet_id"):
        try:
            row = _build_log_row(cfg, ctx, columns_key="log_sheet")
            resp = sc.append_log_row(client.creds, log_cfg, row)
            print(f"  [log] {resp.get('updatedRange')}")
        except Exception:
            log.exception("tracker append failed")

    if label_replied:
        client.add_label_to_thread(tid, label_replied)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run_once(cfg: dict, dry_run: bool = False) -> int:
    client = gm.make_client(cfg, base_dir=HERE)

    planhat = None
    ph = cfg.get("planhat") or {}
    if ph.get("token_path"):
        try:
            planhat = PlanhatClient(
                token_path=str(HERE / ph["token_path"]),
                base_url=ph.get("base_url", "https://api.planhat.com"),
            )
        except Exception:
            log.exception("planhat init failed — running without CSM lookup")

    label_replied = None if dry_run else client.ensure_label(cfg["auto_replied_label"])
    label_manual  = None if dry_run else client.ensure_label(cfg["auto_logged_manual_label"])

    query = cfg.get("extra_query", "").strip()
    print(f"[poll] query: {query}  dry_run={dry_run}")
    matches = client.search(query)
    print(f"[poll] {len(matches)} trigger message(s)")

    seen: set[str] = set()
    for m in matches:
        tid = m.get("threadId")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        try:
            process_thread(
                client, tid, cfg, label_replied, label_manual,
                planhat=planhat, dry_run=dry_run,
            )
        except Exception:
            log.exception("thread %s processing failed", tid)

    try:
        reminders.run_reminders(cfg, base_dir=HERE, dry_run=dry_run)
    except Exception:
        log.exception("reminder pass failed")

    return len(seen)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config", default=str(HERE / "config_worldline.yaml"))
    args = ap.parse_args()

    setup_logging(log_dir=HERE / "logs")
    cfg = load_config(Path(args.config))
    if args.loop:
        interval = int(cfg.get("poll_interval_seconds", 3600))
        log.info("loop start: interval=%ds dry_run=%s", interval, args.dry_run)
        while True:
            try:
                run_once(cfg, dry_run=args.dry_run)
            except Exception:
                log.exception("run_once failed — retrying after interval")
            time.sleep(interval)
    else:
        log.info("one-shot run start: dry_run=%s", args.dry_run)
        run_once(cfg, dry_run=args.dry_run)
        log.info("one-shot run done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
