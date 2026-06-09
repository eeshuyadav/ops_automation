"""
PayU First-Level Chargeback automation entry point.

Triggered on team-forwarded mails matching the PayU notification pattern.
For each new thread:

    1. Parse the xlsx attachment to extract one row per active 'New' case
       (Phone, Transaction id, Amount, Customer name, etc.)
    2. Extract the merchant URL from the forwarded body (`URL : ...`).
    3. Look up the URL in the Merchant Onboarding sheet -> merchant email + entity.
    4. Use the entity to find a CSM in Planhat. If found, Cc the CSM.
       If not found, still send (just without CSM on Cc).
    5. Render templates/reply_payu.html.j2 and send the reply in the same
       thread, internal team + PayU-Chargeback@payu.in always on Cc.
    6. Log a row to the monthly PayU tracker tab.
    7. Apply the `auto-replied-payu` label.

When the merchant URL cannot be resolved to an email, the thread is logged
to the manual-review tracker and labeled `auto-logged-manual-payu` instead
of sending the reply.

After the auto-reply pass, the shared reminder pass runs (`reminders.py`)
so the 48-hour gentle-reminder feature works for PayU too.
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
import merchant_onboarding as mo
import payu_attachment as pa
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


def dedupe_addrs(addrs: list[str], drop: set[str]) -> list[str]:
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


# ---------------------------------------------------------------------------
# Body parsing: pull "URL : https://..." out of the forwarded section.
# ---------------------------------------------------------------------------

_URL_LINE_RE = re.compile(
    r"URL\s*[:\-]\s*(https?://\S+|\S+\.\S+)", re.IGNORECASE,
)


def extract_url_from_body(html_or_text: str) -> str:
    """Find the merchant URL printed in the PayU forwarded body line:
        URL : https://myfrido.com/

    Falls back to scanning for any href= attribute that looks like a merchant
    URL if the labeled line is missing. Returns "" when nothing plausible
    is found.
    """
    if not html_or_text:
        return ""
    # Strip HTML tags very loosely so the regex can match the visible text.
    text = re.sub(r"<[^>]+>", " ", html_or_text)
    text = re.sub(r"&nbsp;", " ", text)
    m = _URL_LINE_RE.search(text)
    if m:
        return m.group(1).strip().rstrip(".,")
    return ""


# ---------------------------------------------------------------------------
# Forwarded-To fallback. When the Onboarding sheet has no row for the URL,
# we extract merchant addresses from the original PayU mail's 'To:' line
# embedded in the forwarded body (the team's "FYR -" forward preserves it).
# Filters out our internal domains so only merchant @-domains survive.
# ---------------------------------------------------------------------------
_FWD_TO_RE = re.compile(
    r"\bTo:\s*(.*?)(?=\b(?:Cc|Subject)\s*:)", re.DOTALL | re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def extract_forwarded_merchant_tos(text: str, cfg: dict) -> list[str]:
    """Return non-internal email addresses found in the forwarded body's
    'To:' block. Used as the merchant-TO fallback when the Onboarding sheet
    has no Website match for the chargeback URL."""
    if not text:
        return []
    # Strip HTML for predictable regex matching.
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"&nbsp;", " ", plain)
    m = _FWD_TO_RE.search(plain)
    if not m:
        return []
    block = m.group(1)
    internal = {d.lower() for d in cfg.get("internal_domains", ["gokwik.co"])}
    self_email = cfg["self_email"].lower()
    out: list[str] = []
    seen: set[str] = set()
    for addr in _EMAIL_RE.findall(block):
        al = addr.lower()
        if al == self_email or al in seen:
            continue
        dom = al.split("@", 1)[1] if "@" in al else ""
        if any(dom == d or dom.endswith("." + d) for d in internal):
            continue
        seen.add(al)
        out.append(addr)
    return out


# ---------------------------------------------------------------------------
# Attachment fetch
# ---------------------------------------------------------------------------

def find_xls_attachment_bytes(client, msg: dict) -> bytes:
    """Walk the message payload and return the first xls/xlsx attachment
    body as bytes. Returns b"" if none is present."""
    import base64

    def walk(part):
        fname = (part.get("filename") or "").lower()
        if fname.endswith(".xls") or fname.endswith(".xlsx"):
            body = part.get("body", {}) or {}
            data = body.get("data")
            if not data and body.get("attachmentId"):
                att = client.svc.users().messages().attachments().get(
                    userId="me", messageId=msg["id"], id=body["attachmentId"],
                ).execute()
                data = att.get("data", "")
            if data:
                return base64.urlsafe_b64decode(data.encode() + b"==")
        for p in part.get("parts", []) or []:
            r = walk(p)
            if r:
                return r
        return b""

    return walk(msg.get("payload", {}))


# ---------------------------------------------------------------------------
# CSM lookup via Planhat
# ---------------------------------------------------------------------------

def lookup_csm(planhat: PlanhatClient | None, entity_name: str) -> list[str]:
    """Return owner + coOwner emails for the merchant whose entity name
    matches `entity_name`, or [] when not found / no Planhat configured.
    Same call shape that the Easebuzz flow uses."""
    if not planhat or not entity_name:
        return []
    try:
        return planhat.get_csm_emails(entity_name) or []
    except Exception:
        log.exception("planhat lookup failed for %r", entity_name)
        return []


# ---------------------------------------------------------------------------
# Tracker row builder (mirrors main.py:_build_log_row in /1/)
# ---------------------------------------------------------------------------

def _build_log_row(cfg: dict, fields: dict, *, columns_key: str = "log_sheet") -> list[str]:
    import datetime
    today = datetime.datetime.now().strftime("%-d-%b-%Y")
    values: list[str] = []
    for spec in cfg[columns_key].get("columns", []):
        if spec == "__today__":
            values.append(today)
        elif spec.startswith("__static__:"):
            values.append(spec.split(":", 1)[1])
        else:
            values.append(str(fields.get(spec) or ""))
    return values


# ---------------------------------------------------------------------------
# Main processing per thread
# ---------------------------------------------------------------------------

def process_thread(
    client,
    tid: str,
    cfg: dict,
    label_replied: str | None,
    label_manual: str | None,
    merchant_lookup: dict,
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
    print(f"[thread] {tid} subject={subject!r} msgs={len(msgs)}")

    # Gate 1: subject regex
    pat = cfg.get("subject_regex")
    if pat and not re.search(pat, subject, re.IGNORECASE):
        print("  [skip] subject regex did not match")
        return

    # Gate 2: skip if anyone already replied to the forwarded mail
    if len(msgs) > 1:
        print(f"  [skip] thread already has {len(msgs)} messages — no auto-reply")
        return

    # Gate 3: identity — accept the mail if it's FROM an allowed sender
    # (PayU direct) OR if the legacy team-forwarded body has the PayU origin
    # mentioned. New PayU flow ships directly from PayU-Chargeback@payu.in
    # so the From check alone is sufficient.
    body_html = gm.message_html_body(first)
    body_text = body_html
    allowed = {s.lower() for s in (cfg.get("allowed_senders") or [])}
    from_addrs = [a.lower() for a in split_addrs(gm.header(first, "From"))]
    if not (allowed and any(a in allowed for a in from_addrs)):
        # Fall back to the old "PayU origin must be in body" check for legacy
        # team-forwarded mails. If neither path matches, reject.
        fwd_from = cfg.get("required_forwarded_from", "")
        if not (fwd_from and fwd_from.lower() in body_text.lower()):
            print(f"  [skip] sender {from_addrs} not allowed and PayU origin "
                  f"not in body")
            return

    # Gate 4: extract the merchant URL
    merchant_url = extract_url_from_body(body_text)
    if not merchant_url:
        print("  [skip] could not extract merchant URL from body")
        return
    print(f"  url={merchant_url!r}")

    # Gate 5: download + parse the attachment
    att_bytes = find_xls_attachment_bytes(client, first)
    if not att_bytes:
        print("  [skip] no xls/xlsx attachment on this mail")
        return
    cases = pa.parse_attachment(att_bytes)
    if not cases:
        print("  [skip] attachment has no rows under the 'New' sheet")
        return
    print(f"  cases in 'New' sheet: {len(cases)}")

    # Gate 6: resolve merchant. Sheet match first (preferred), then fall back
    # to the merchant addresses preserved in the forwarded body's To: line.
    block = mo.resolve(merchant_url, merchant_lookup)
    merchant_email_sheet = (block.get("email") or "").strip()
    merchant_entity = (block.get("entity") or "").strip()
    merchant_brand = (block.get("brand") or "").strip()

    if merchant_email_sheet:
        merchant_tos = [merchant_email_sheet]
        print(f"  sheet hit: email={merchant_email_sheet}  entity={merchant_entity!r}  brand={merchant_brand!r}")
    else:
        merchant_tos = extract_forwarded_merchant_tos(body_text, cfg)
        if merchant_tos:
            print(f"  sheet miss for {merchant_url!r} — forwarded-To fallback: "
                  f"{merchant_tos}  entity={merchant_entity!r}")
        else:
            print(f"  [manual] sheet miss AND no forwarded-To fallback for "
                  f"{merchant_url!r}")
            for c in cases:
                fields = {
                    **c,
                    "merchant_url":    merchant_url,
                    "merchant_email_id": "",
                    "merchant_entity":  "",
                }
                if dry_run:
                    print(f"    [DRY-RUN] would log to manual tracker: case={c.get('case_number')}")
                    continue
                try:
                    row = _build_log_row(cfg, fields, columns_key="failed_log_sheet")
                    resp = sc.append_log_row(client.creds, cfg["failed_log_sheet"], row)
                    print(f"    [manual-log] {resp.get('updatedRange')}")
                except Exception:
                    log.exception("manual log failed")
            if not dry_run and label_manual:
                client.add_label_to_thread(tid, label_manual)
            return

    # CSM via Planhat — only when we have an entity name from the sheet.
    # If sheet missed, we have no entity name → no CSM (per the agreed
    # "still send" fallback). Best-effort either way.
    csm_emails = lookup_csm(planhat, merchant_entity) if merchant_entity else []
    if csm_emails:
        print(f"  csm: {csm_emails}")
    else:
        print(f"  csm: (none — replying without CSM on Cc)")

    # Build Cc list: internal_always_cc + addresses to preserve from the
    # original thread (only if they were actually on it) + CSM, dedupe,
    # drop self + all merchant TO addresses.
    self_email = cfg["self_email"]
    orig_addrs = {a.lower() for a in
                  split_addrs(gm.header(first, "To")) +
                  split_addrs(gm.header(first, "Cc"))}
    preserved = [a for a in (cfg.get("preserve_from_thread") or [])
                 if a.lower() in orig_addrs]
    cc_list = dedupe_addrs(
        (cfg.get("internal_always_cc") or []) + preserved + csm_emails,
        drop={self_email} | {m.lower() for m in merchant_tos},
    )

    # Subject: reuse forwarded subject (already starts with "Fwd: ..."), but
    # prefix with "Re:" if not already a reply.
    subject_tpl = cfg["reply"].get("subject_override") or subject
    if not re.match(r"^\s*re:", subject_tpl, re.IGNORECASE):
        subject_tpl = f"Re: {subject_tpl}"

    last_msgid = gm.header(msgs[-1], "Message-ID")
    template_rel = cfg["reply"]["template_path"]

    # One reply per case row. In practice TOTAL CASES is usually 1, but if
    # PayU bundles multiple rows in one mail, we send one reply per row so
    # each Phone+Payment id pair is mailed cleanly.
    # Pick a human-friendly brand name for the reply body. Order:
    #   1) Sheet's Merchant Name (col F) if we hit the sheet
    #   2) URL apex stem (e.g. "https://myfrido.com/" -> "myfrido")
    def _brand_from_url(u: str) -> str:
        from urllib.parse import urlparse
        try:
            netloc = urlparse(u).netloc or u.split("://", 1)[-1]
            netloc = netloc.split("/", 1)[0].lower()
            if netloc.startswith("www."):
                netloc = netloc[4:]
            stem = netloc.split(".", 1)[0]
            return stem.title() if stem else ""
        except Exception:
            return ""

    merchant_name_for_body = merchant_brand or _brand_from_url(merchant_url)

    to_hdr = ", ".join(merchant_tos)
    cc_hdr = ", ".join(cc_list)

    # ONE mail per thread (not one per case). The reply template loops over
    # the `cases` list inline, so the merchant gets a single mail with all
    # cases listed + the original xls attached.
    ctx = {
        "merchant_url":      merchant_url,
        "merchant_email_id": merchant_tos[0],
        "merchant_entity":   merchant_entity,
        "merchant_name":     merchant_name_for_body,
        "cases":             cases,
        "self_email":        self_email,
        "self_display_name": cfg.get("self_display_name", ""),
    }
    html = render(template_rel, ctx)

    if dry_run:
        print(f"  [DRY-RUN] {len(cases)} case(s) bundled into 1 reply")
        print(f"    To:  {to_hdr}")
        print(f"    Cc:  {cc_hdr}")
        print(f"    Sub: {subject_tpl}")
        print(f"    Attach: chargeback.xls ({len(att_bytes)} bytes)")
        return

    client.send_reply(
        to=to_hdr,
        cc=cc_hdr,
        subject=subject_tpl,
        html_body=html,
        thread_id=tid if cfg["reply"].get("reply_in_thread", True) else None,
        in_reply_to=last_msgid or None,
        attachments=[("chargeback.xls", att_bytes, "vnd.ms-excel")],
    )
    print(f"  [sent] to={to_hdr}  cases={len(cases)}  attach={len(att_bytes)} bytes")

    if label_replied:
        client.add_label_to_thread(tid, label_replied)

    # Tracker — one row PER case (so each chargeback shows up individually).
    log_cfg = cfg.get("log_sheet") or {}
    if log_cfg.get("spreadsheet_id"):
        for c in cases:
            ctx_log = {
                **c,
                "merchant_email_id": merchant_tos[0],
                "merchant_entity":   merchant_entity,
                "merchant_name":     merchant_name_for_body,
            }
            try:
                row = _build_log_row(cfg, ctx_log, columns_key="log_sheet")
                resp = sc.append_log_row(client.creds, log_cfg, row)
                print(f"    [log] case={c.get('case_number')}  {resp.get('updatedRange')}")
            except Exception:
                log.exception("tracker append failed for case %s",
                              c.get("case_number"))


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run_once(cfg: dict, dry_run: bool = False) -> int:
    client = gm.make_client(cfg, base_dir=HERE)

    # Pre-build the merchant lookup once per tick.
    try:
        merchant_lookup = mo.build_lookup(client.creds, cfg["merchant_sheet"])
        print(f"[onboarding] loaded {len(merchant_lookup)} merchant URL row(s)")
    except Exception as e:
        print(f"[onboarding-error] {e!r}", file=sys.stderr)
        merchant_lookup = {}

    # Planhat best-effort
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

    seen_threads: set[str] = set()
    for m in matches:
        tid = m.get("threadId")
        if not tid or tid in seen_threads:
            continue
        seen_threads.add(tid)
        try:
            process_thread(
                client, tid, cfg, label_replied, label_manual, merchant_lookup,
                planhat=planhat, dry_run=dry_run,
            )
        except Exception:
            log.exception("thread %s processing failed", tid)

    # Reminder pass (shared across all flows via reminders.py)
    try:
        reminders.run_reminders(cfg, base_dir=HERE, dry_run=dry_run)
    except Exception:
        log.exception("reminder pass failed")

    return len(seen_threads)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config", default=str(HERE / "config_payu.yaml"))
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
