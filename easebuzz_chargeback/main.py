"""
Chargeback auto-reply.

Watches the `chargeback-automation` mailbox for unread Easebuzz chargeback
mails, extracts customer fields from the HTML body, looks up the merchant's
routing info from a Google Sheet, and sends the Gokwik-side follow-up reply.

Auth: user OAuth. One-time browser consent as the bot mailbox generates
token.json — refresh token carries both Gmail + Sheets scopes. From then on
the script runs unattended.

Setup:
  1. `pip install -r requirements.txt`
  2. IT provides credentials.json (OAuth Desktop client, Internal consent,
     Gmail + Sheets APIs enabled).
  3. On a machine with a browser, run `python main.py --once` and sign in
     as the bot mailbox when the browser opens. token.json is written.
  4. Ship credentials.json + token.json to the server.
  5. Run `python main.py --loop` on the server.
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

import body_parser as bp
import gmail_client as gm
import reminders
import sheets_client as sc
from logging_setup import log, setup_logging
from planhat_client import PlanhatClient

HERE = Path(__file__).parent


# --- helpers ----------------------------------------------------------------

def load_config(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f)


def split_addrs(raw: str) -> list[str]:
    return [addr for _name, addr in getaddresses([raw]) if addr]


def dedupe(addrs: list[str], drop: set[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    drop_l = {a.lower() for a in drop}
    for a in addrs:
        key = a.lower()
        if key in drop_l or key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def domain_of(addr: str) -> str:
    return addr.split("@", 1)[1].lower() if "@" in addr else ""


def normalize_name(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"[\s._\-'\"/&]+", "", s).lower()


def _partial_sheet_match(key: str, sheet_lookup: dict, min_len: int = 5) -> dict:
    """Bidirectional prefix match on the normalized name.

    Accepts a hit when the extracted merchant name is a prefix of a sheet
    key (mail has short trading name, sheet has legal "X Private Limited"
    form), or vice versa (sheet row is shorter than mail salutation).

    `min_len` gates false positives on short tokens — the shorter side
    must be at least this many chars. If multiple candidates qualify,
    the one with the longest common prefix wins.
    """
    if not key or len(key) < min_len:
        return {}
    candidates: list[tuple[int, str, dict]] = []  # (overlap_len, sheet_key, block)
    for sk, block in sheet_lookup.items():
        if not sk:
            continue
        short = sk if len(sk) < len(key) else key
        if len(short) < min_len:
            continue
        if sk.startswith(key) or key.startswith(sk):
            candidates.append((len(short), sk, block))
    if not candidates:
        return {}
    candidates.sort(reverse=True)  # longest overlap first
    best = candidates[0]
    print(f"  [sheet] partial match: mail={key!r} -> sheet={best[1]!r}")
    return best[2]


def _match_by_email(sheet_lookup: dict, email: str) -> dict:
    """Find a sheet row whose Email ID 1 matches the given email address."""
    target = (email or "").lower().strip()
    if not target:
        return {}
    for sk, block in sheet_lookup.items():
        for e in block.get("extra_to", []) or []:
            if e.lower().strip() == target:
                print(f"  [sheet] email match: {target!r} -> sheet row {block.get('raw_name')!r}")
                return block
    return {}


def lookup_merchant_contacts(
    cfg: dict,
    sheet_lookup: dict,
    *,
    domain: str,
    name: str | None,
    orig_to_email: str | None = None,
) -> dict:
    """Resolve merchant contacts via tiers, stopping at first hit:
      1. Exact normalized-name match in the sheet (zero false positives)
      2. Email match — mail's original To matches a sheet row's Email ID 1
         (catches name mismatches like "Zeraki India" in the sheet vs
          "ZERAKI MARKETING PRIVATE LIMITED" in the mail)
      3. Prefix name match (e.g., "Vastramay" <-> "Vastramay Private Limited")
      4. Static `merchant_contacts` block in config.yaml
    """
    key = normalize_name(name)
    if key and key in sheet_lookup:
        return sheet_lookup[key]
    hit = _match_by_email(sheet_lookup, orig_to_email or "")
    if hit:
        return hit
    hit = _partial_sheet_match(key, sheet_lookup)
    if hit:
        return hit
    # static fallback
    mc = cfg.get("merchant_contacts", {}) or {}
    for k, block in mc.items():
        if "@" not in k and "." in k and k.lower() == (domain or "").lower():
            return block or {}
    if key:
        for k, block in mc.items():
            if "." in k:
                continue
            if normalize_name(k) == key:
                return block or {}
    return {}


def render(template_path: Path, ctx: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        autoescape=select_autoescape(["html", "htm", "j2"]),
    )
    return env.get_template(template_path.name).render(**ctx)


def render_subject(subject_template: str, ctx: dict) -> str:
    return Environment(autoescape=False).from_string(subject_template).render(**ctx)


# --- core -------------------------------------------------------------------

def aggregate_fields(thread: dict, cfg: dict, watch_senders) -> dict:
    """Walk all messages in the thread originating from any of the configured
    chargeback-team senders; return the union of extracted fields. Chargeback
    info is pure HTML — no PDF handling.

    `watch_senders` may be a single string (legacy) or a list of addresses.
    """
    if isinstance(watch_senders, str):
        watch_senders = [watch_senders]
    valid = {a.lower() for a in (watch_senders or []) if a}

    labels = cfg["body_table_labels"]
    fields: dict[str, str | None] = {k: None for k in labels}

    for msg in thread.get("messages", []):
        from_addrs = [a.lower() for a in split_addrs(gm.header(msg, "From"))]
        if not from_addrs or from_addrs[0] not in valid:
            continue
        html = gm.message_html_body(msg)
        if not html:
            continue
        for k, v in bp.parse_body_fields(html, labels).items():
            if fields.get(k) is None and v is not None:
                fields[k] = v

    return fields


def compute_addresses(
    thread: dict,
    cfg: dict,
    sheet_lookup: dict,
    watch_senders,
    merchant_name: str | None = None,
    planhat: PlanhatClient | None = None,
) -> tuple[str, str]:
    """Return (to_header, cc_header) strings for the outgoing reply.
    `watch_senders` may be a single string (legacy) or a list of addresses."""
    if isinstance(watch_senders, str):
        watch_senders = [watch_senders]
    valid = {a.lower() for a in (watch_senders or []) if a}

    anchor = None
    for msg in thread.get("messages", []):
        from_addrs = [a.lower() for a in split_addrs(gm.header(msg, "From"))]
        if from_addrs and from_addrs[0] in valid:
            anchor = msg
            break
    if anchor is None:
        return "", ""

    orig_from = split_addrs(gm.header(anchor, "From"))
    orig_to = split_addrs(gm.header(anchor, "To"))
    orig_cc = split_addrs(gm.header(anchor, "Cc"))

    self_email = cfg["self_email"]

    dom = domain_of(orig_to[0]) if orig_to else ""
    orig_to_first = orig_to[0] if orig_to else ""
    block = lookup_merchant_contacts(
        cfg, sheet_lookup,
        domain=dom, name=merchant_name, orig_to_email=orig_to_first,
    )
    extra_to = list(block.get("extra_to") or [])
    extra_cc = list(block.get("extra_cc") or [])

    # If the merchant's sheet Status matches a skip pattern (e.g.,
    # "Transferred to Growth"), drop the sheet-provided CSM — they're no
    # longer the right person for this merchant.
    status = (block.get("status") or "").lower()
    skip_pats = [p.lower() for p in cfg.get("sheet", {}).get("status_skip_csm_patterns", [])]
    if status and any(p in status for p in skip_pats):
        if extra_cc:
            log.info("sheet: dropping CSM %r because status=%r", extra_cc, status)
        extra_cc = []

    # Planhat is a FALLBACK — only used when the sheet has no CSM for this
    # merchant (either no row at all, or the row's status was Transferred so
    # the CSM was dropped above). When it does kick in, both `owner` and
    # `coOwner` emails are added to Cc.
    if not extra_cc and planhat:
        try:
            planhat_emails = planhat.get_csm_emails(merchant_name, orig_to_first)
        except Exception:
            log.exception("planhat lookup failed for %r", merchant_name)
            planhat_emails = []
        if planhat_emails:
            log.info("planhat fallback: %r -> %s", merchant_name, planhat_emails)
            extra_cc = list(planhat_emails)
        else:
            log.info("planhat fallback: no record for %r — replying without CSM",
                     merchant_name)

    # Drop self-domain addresses (e.g., satabdi+908@gokwik.co) from the To
    # list — the reply "To" is for merchant-side contacts. Internal folks
    # still get the reply via internal_always_cc / original Cc.
    self_domain = (self_email.split("@", 1)[1].lower() if "@" in self_email else "")
    if self_domain:
        orig_to_before = list(orig_to)
        orig_to = [a for a in orig_to if a.split("@", 1)[-1].lower() != self_domain]
        dropped = [a for a in orig_to_before if a not in orig_to]
        if dropped:
            print(f"  [route] dropping self-domain addrs from To: {dropped}")
    to_list = dedupe(orig_to + extra_to, drop={self_email})
    cc_list = dedupe(
        orig_from + orig_cc + extra_cc + list(cfg.get("internal_always_cc", [])),
        drop={self_email, *to_list},
    )
    return ", ".join(to_list), ", ".join(cc_list)


def process_thread(client, thread_id: str, cfg: dict, label_id: str, sheet_lookup: dict,
                   planhat: PlanhatClient | None = None, dry_run: bool = False) -> None:
    thread = client.get_thread(thread_id)
    # Senders the bot trusts as the chargeback team. New `watch_senders` list
    # takes precedence; falls back to legacy single `watch_sender` if present.
    watch_sender = cfg.get("watch_senders") or cfg.get("watch_sender")

    if not thread.get("messages"):
        print(f"  [skip] empty thread {thread_id}")
        return

    first = thread["messages"][0]
    thread_subject = gm.header(first, "Subject") or "(no subject)"
    thread_msg_ids = [m["id"] for m in thread["messages"]]

    print(f"[thread] {thread_id} subject={thread_subject!r} msgs={len(thread_msg_ids)}")

    # Strict code-level subject format check. Rejects anything that passed
    # the loose Gmail substring query but isn't the exact "Chargeback Raised:
    # <id> - Action Required" shape (e.g. "Chargeback Escalation: … Urgent
    # Action Required!").
    subject_regex = cfg.get("subject_regex")
    if subject_regex and not re.search(subject_regex, thread_subject, re.IGNORECASE):
        print(f"  [skip] subject doesn't match the chargeback format regex")
        return

    # Identity check — sender/reply-to headers must match. Catches attempts
    # to spoof the subject line from a different address.
    #
    # Accepts a single value OR a list:
    #     required_from: "accountsreceivable@gokwik.co"      # legacy
    #     required_from:                                      # new
    #       - "accountsreceivable@gokwik.co"
    #       - "chargeback@easebuzz.in"
    #
    # When the From already matches one of the AUTHORITATIVE senders (defined
    # in `authoritative_senders`), the Reply-To gate is skipped. This handles
    # the case where Easebuzz sends directly (From: chargeback@easebuzz.in
    # with no Reply-To header) instead of via the Accounts Receivable group.
    def _as_list(v):
        return v if isinstance(v, list) else ([v] if v else [])

    from_addrs = [a.lower() for a in split_addrs(gm.header(first, "From"))]
    allowed_from = [a.lower() for a in _as_list(cfg.get("required_from"))]
    if allowed_from:
        if not any(a in from_addrs for a in allowed_from):
            print(f"  [skip] From header doesn't include any of {allowed_from!r} (got {from_addrs})")
            return

    authoritative = {a.lower() for a in _as_list(cfg.get("authoritative_senders"))}
    is_authoritative = any(a in authoritative for a in from_addrs)

    allowed_reply_to = [a.lower() for a in _as_list(cfg.get("required_reply_to"))]
    if allowed_reply_to and not is_authoritative:
        reply_to_addrs = [a.lower() for a in split_addrs(gm.header(first, "Reply-To"))]
        if not any(a in reply_to_addrs for a in allowed_reply_to):
            print(f"  [skip] Reply-To doesn't include any of {allowed_reply_to!r} (got {reply_to_addrs})")
            return

    # Dedup (thread-level): only reply when the thread contains one message.
    if len(thread.get("messages", [])) > 1:
        print(f"  [skip] thread already has {len(thread['messages'])} messages "
              f"(someone replied) — no auto-reply")
        return

    # Dedup (body-level): catch cases where an entire conversation was
    # forwarded into the bot mailbox as a single message — Gmail's thread
    # view counts it as 1 msg but the body clearly contains prior replies.
    first_html = gm.message_html_body(first)
    if bp.has_quoted_reply(first_html):
        print("  [skip] body contains quoted reply / forward — conversation in progress")
        return

    fields = aggregate_fields(thread, cfg, watch_sender)

    required = cfg.get("reply", {}).get("required_fields", []) or []
    missing = [k for k in required if not fields.get(k)]
    if missing:
        print(f"  [skip] required fields missing: {missing} — retry next poll")
        return

    to_hdr, cc_hdr = compute_addresses(
        thread, cfg, sheet_lookup, watch_sender,
        merchant_name=fields.get("merchant_name"),
        planhat=planhat,
    )
    if not to_hdr:
        print("  [skip] could not resolve recipients — logging to manual tracker")
        if not dry_run:
            _append_failed_row(cfg, client.creds, fields, sheet_lookup,
                                planhat=planhat, thread=thread)
            # Apply a label so cron doesn't re-log this thread on every tick.
            ml_label = cfg.get("auto_logged_manual_label")
            if ml_label:
                try:
                    lid = client.ensure_label(ml_label)
                    client.add_label_to_thread(thread_id, lid)
                    log.info("manual-log: applied label %r to thread %s",
                             ml_label, thread_id)
                except Exception:
                    log.exception("failed to apply manual-logged label")
        return

    # Note: we do NOT block the reply when the sheet has no CSM. The mail
    # still goes out — just without the CSM on Cc. The tracker-log step
    # below still gets the merchant contact (via extra_to / email match).
    orig_first = thread["messages"][0]
    orig_to_addrs = split_addrs(gm.header(orig_first, "To"))
    dom = orig_to_addrs[0].split("@", 1)[1].lower() if orig_to_addrs else ""
    block = lookup_merchant_contacts(
        cfg, sheet_lookup,
        domain=dom, name=fields.get("merchant_name"),
        orig_to_email=orig_to_addrs[0] if orig_to_addrs else "",
    )
    if not (block.get("extra_cc") or []):
        print(f"  [warn] no CSM in sheet for merchant "
              f"{fields.get('merchant_name')!r} — replying without a CSM on Cc")

    last = thread["messages"][-1]
    last_msgid = gm.header(last, "Message-ID")

    ctx = {
        **fields,
        # Strip trailing time off transaction_date so customer-facing copies
        # show "April 4, 2026" instead of "April 4, 2026, 5:20 a.m." — same
        # transform already applied to the tracker row via _strip_time().
        "transaction_date":  _strip_time(str(fields.get("transaction_date") or "")),
        "self_email":        cfg["self_email"],
        "self_display_name": cfg.get("self_display_name", ""),
        "contact_email":     cfg.get("contact_email", ""),
        "contact_phone":     cfg.get("contact_phone", ""),
        "thread_subject":    thread_subject,
    }

    subject_tpl = cfg["reply"].get("subject_override") or thread_subject
    if not re.match(r"^\s*re:", subject_tpl, re.IGNORECASE):
        subject_tpl = f"Re: {subject_tpl}"
    subject = render_subject(subject_tpl, ctx)

    html = render(HERE / cfg["reply"]["template_path"], ctx)

    if dry_run:
        print("  [DRY-RUN] would send:")
        print(f"    To:      {to_hdr}")
        print(f"    Cc:      {cc_hdr}")
        print(f"    Subject: {subject}")
        print(f"    --- body ---\n{html}\n    --- end body ---")
        print("  [DRY-RUN] no mail sent, no label applied, nothing marked read.")
        return

    thread_param = thread_id if cfg["reply"].get("reply_in_thread", True) else None
    client.send_reply(
        to=to_hdr,
        cc=cc_hdr,
        subject=subject,
        html_body=html,
        thread_id=thread_param,
        in_reply_to=last_msgid or None,
    )
    print(f"  [sent] to={to_hdr}")
    print(f"         cc={cc_hdr}")

    client.add_label_to_thread(thread_id, label_id)
    for mid in thread_msg_ids:
        try:
            client.mark_read(mid)
        except Exception:
            pass

    # Log the successful send to the tracker sheet. Non-fatal if it errors —
    # the reply has already gone out.
    log_cfg = cfg.get("log_sheet") or {}
    if log_cfg.get("spreadsheet_id"):
        try:
            row = _build_log_row(cfg, fields, block)
            resp = sc.append_log_row(client.creds, log_cfg, row)
            print(f"  [log] appended to {resp['tab']!r} ({resp.get('updatedRange')})")
        except Exception as e:
            print(f"  [log-error] {type(e).__name__}: {e}", file=sys.stderr)


def _strip_time(s: str) -> str:
    """Drop the trailing time portion from an extracted date string.
    'April 19, 2026, 11:14 p.m.' -> 'April 19, 2026'"""
    if not s:
        return ""
    parts = s.split(",")
    if len(parts) >= 2:
        return ",".join(parts[:2]).strip()
    return s.strip()


def _build_log_row(cfg: dict, fields: dict, block: dict,
                   columns_key: str = "log_sheet") -> list[str]:
    """Construct a row for the tracker sheet using the columns mapping at
    cfg[columns_key].columns. Use columns_key='log_sheet' for successful
    sends, 'failed_log_sheet' for manual-review rows."""
    import datetime
    today = datetime.datetime.now().strftime("%-d-%b-%Y")  # e.g. "26-Apr-2026"
    merchant_email = ""
    extra_to = block.get("extra_to") or []
    if extra_to:
        merchant_email = extra_to[0]

    values: list[str] = []
    for spec in cfg[columns_key].get("columns", []):
        if spec == "__today__":
            values.append(today)
        elif spec.startswith("__static__:"):
            values.append(spec.split(":", 1)[1])
        elif spec == "merchant_email_id":
            values.append(merchant_email)
        elif spec == "transaction_date":
            values.append(_strip_time(str(fields.get(spec) or "")))
        else:
            values.append(str(fields.get(spec) or ""))
    return values


def _append_failed_row(cfg: dict, creds, fields: dict, sheet_lookup: dict,
                       *, planhat: PlanhatClient | None, thread: dict) -> None:
    """Log this thread to the manual-review tracker. Best-effort: any error
    is logged but does not propagate."""
    if not cfg.get("failed_log_sheet"):
        return
    try:
        # Re-resolve the merchant block to get any extra_to we found (could
        # be empty — that's exactly why this row is in the manual tracker).
        first = thread["messages"][0]
        orig_to_addrs = split_addrs(gm.header(first, "To"))
        dom = orig_to_addrs[0].split("@", 1)[1].lower() if orig_to_addrs else ""
        block = lookup_merchant_contacts(
            cfg, sheet_lookup,
            domain=dom, name=fields.get("merchant_name"),
            orig_to_email=orig_to_addrs[0] if orig_to_addrs else "",
        )
        row = _build_log_row(cfg, fields, block, columns_key="failed_log_sheet")
        resp = sc.append_log_row(creds, cfg["failed_log_sheet"], row)
        log.info("manual-log: appended to %r at %s",
                 resp.get("tab"), resp.get("updatedRange"))
    except Exception:
        log.exception("manual-log append failed")


def _build_planhat(cfg: dict) -> PlanhatClient | None:
    """Create the PlanhatClient if config'd; return None on missing config or
    init failure (so the pipeline still works without Planhat)."""
    ph = cfg.get("planhat") or {}
    token_path = ph.get("token_path")
    if not token_path:
        return None
    try:
        return PlanhatClient(
            token_path=str(HERE / token_path),
            base_url=ph.get("base_url", "https://api.planhat.com"),
        )
    except Exception:
        log.exception("planhat init failed — running without Planhat fallback")
        return None


def build_sheet_lookup(cfg: dict, creds) -> dict:
    """Load the merchant-contacts lookup from the Google Sheet using the
    shared OAuth creds. Returns {} on any failure — the pipeline then
    falls back to `merchant_contacts` in config.yaml."""
    try:
        lookup = sc.merchant_lookup(cfg.get("sheet") or {}, creds=creds, base_dir=HERE)
        if lookup:
            print(f"[sheet] loaded {len(lookup)} merchant rows")
        return lookup
    except Exception as e:
        print(f"[sheet-error] {e!r}", file=sys.stderr)
        return {}


def run_once(cfg: dict, dry_run: bool = False) -> int:
    client = gm.make_client(cfg, base_dir=HERE)
    sheet_lookup = build_sheet_lookup(cfg, creds=client.creds)
    planhat = _build_planhat(cfg)
    # Don't auto-create labels in dry-run — stays purely read-only.
    label_id = None if dry_run else client.ensure_label(cfg["auto_replied_label"])
    # Build a Gmail search that matches any of `watch_senders` (list) plus
    # the optional extra_query. Falls back to legacy single `watch_sender`.
    senders = cfg.get("watch_senders")
    if not senders:
        senders = [cfg["watch_sender"]] if cfg.get("watch_sender") else []
    if senders:
        from_clause = "{" + " ".join(f"from:{s}" for s in senders) + "}"
    else:
        from_clause = ""
    query = f'{from_clause} {cfg.get("extra_query", "")}'.strip()
    print(f"[poll] query: {query}  dry_run={dry_run}")
    messages = client.search(query)
    print(f"[poll] {len(messages)} trigger message(s)")

    seen_threads: set[str] = set()
    for m in messages:
        full = client.get_message(m["id"])
        tid = full.get("threadId")
        if not tid or tid in seen_threads:
            continue
        seen_threads.add(tid)
        try:
            process_thread(client, tid, cfg, label_id, sheet_lookup,
                           planhat=planhat, dry_run=dry_run)
        except Exception:
            log.exception("thread %s processing failed", tid)
    try:
        client.close()
    except Exception:
        pass

    # After processing new mails, run the +N-day reminder pass for this same
    # config. No-op when cfg lacks `auto_reminded_label`.
    try:
        reminders.run_reminders(cfg, base_dir=HERE, dry_run=dry_run)
    except Exception:
        log.exception("reminder pass failed")

    return len(seen_threads)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch + parse + preview replies but never send mail, "
                         "never apply labels, never mark anything read.")
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    args = ap.parse_args()

    setup_logging(log_dir=HERE / "logs")
    cfg = load_config(Path(args.config))
    if args.loop:
        interval = int(cfg.get("poll_interval_seconds", 60))
        log.info("loop start: interval=%ds dry_run=%s", interval, args.dry_run)
        while True:
            try:
                run_once(cfg, dry_run=args.dry_run)
            except Exception:
                log.exception("run_once failed — will retry after interval")
            time.sleep(interval)
    else:
        log.info("one-shot run start: dry_run=%s", args.dry_run)
        run_once(cfg, dry_run=args.dry_run)
        log.info("one-shot run done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
