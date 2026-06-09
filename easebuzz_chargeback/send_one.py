"""Manual one-off send for a specific chargeback transaction.

Runs locally (uses your local credentials.json + token.json + planhat_token.txt
+ config.yaml). Same pipeline as the production cron — all 8 safety gates,
sheet + Planhat lookup, reply, label, mark-read, tracker log — but scoped
to ONE transaction ID you specify.

Usage:
    python3 send_one.py --dry-run E2604190X9TF61   # preview only, don't send
    python3 send_one.py E2604190X9TF61              # actually send + log

The script bypasses the `after:` date filter so you can manually process
older mails the hourly cron is configured to skip.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

import body_parser as bp
import gmail_client as gm
import sheets_client as sc
from main import (
    aggregate_fields,
    compute_addresses,
    lookup_merchant_contacts,
    render,
    render_subject,
    split_addrs,
    _build_planhat,
    _build_log_row,
)

HERE = Path(__file__).parent


def fail(reason: str) -> int:
    print(f"\n❌ ABORT — {reason}")
    print("(no mail sent, no label applied, nothing marked read, no row logged)")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("transaction_id", help="e.g. E2604190X9TF61")
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview only — runs all gates but does not send.")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    client = gm.make_client(cfg, base_dir=HERE)
    sheet_lookup = sc.merchant_lookup(cfg["sheet"], creds=client.creds)
    planhat = _build_planhat(cfg)

    txn = args.transaction_id
    # Use the production sender filter but DROP the `after:` date filter so we
    # can process older mails too.
    query = f'from:{cfg["watch_sender"]} subject:{txn}'
    matches = client.search(query)
    print(f"== Gmail search: {query!r} -> {len(matches)} match(es)")
    if not matches:
        return fail(f"transaction {txn!r} not found in inbox from the watched sender")

    full = client.get_message(matches[0]["id"])
    thread_id = full["threadId"]
    thread = client.get_thread(thread_id)
    first = thread["messages"][0]
    subj = gm.header(first, "Subject")
    thread_msg_ids = [m["id"] for m in thread["messages"]]

    print(f"   thread:  {thread_id}")
    print(f"   subject: {subj!r}")
    print()
    print("---- safety gates ----")

    # Gate 1
    pat = cfg.get("subject_regex")
    if pat and not re.search(pat, subj, re.IGNORECASE):
        return fail(f"subject regex {pat!r} didn't match")
    print("[1] subject regex                 OK")

    # Gate 2
    from_addrs = [a.lower() for a in split_addrs(gm.header(first, "From"))]
    rf = cfg.get("required_from", "").lower()
    if rf and rf not in from_addrs:
        return fail(f"From header doesn't include {rf!r} (got {from_addrs})")
    print("[2] required_from header          OK")

    # Gate 3
    reply_to_addrs = [a.lower() for a in split_addrs(gm.header(first, "Reply-To"))]
    rrt = cfg.get("required_reply_to", "").lower()
    if rrt and rrt not in reply_to_addrs:
        return fail(f"Reply-To doesn't include {rrt!r} (got {reply_to_addrs})")
    print("[3] required_reply_to header      OK")

    # Gate 4
    if "auto-replied" in full.get("labelIds", []):
        return fail("auto-replied label already set — thread already handled")
    print("[4] auto-replied label NOT set    OK")

    # Gate 5
    if len(thread["messages"]) != 1:
        return fail(f"thread has {len(thread['messages'])} messages (someone replied)")
    print("[5] thread has exactly 1 message  OK")

    # Gate 6
    html = gm.message_html_body(first)
    if bp.has_quoted_reply(html):
        return fail("body contains a quoted-reply chain")
    print("[6] body has no quoted chain      OK")

    # Gate 7
    fields = aggregate_fields(thread, cfg, cfg["watch_sender"])
    missing = [k for k in cfg["reply"]["required_fields"] if not fields.get(k)]
    if missing:
        return fail(f"required fields missing: {missing}")
    print("[7] required fields extracted     OK")

    # Gate 8
    to_hdr, cc_hdr = compute_addresses(
        thread, cfg, sheet_lookup, cfg["watch_sender"],
        merchant_name=fields.get("merchant_name"),
        planhat=planhat,
    )
    if not to_hdr:
        return fail("no merchant recipients resolved (To is empty after filtering)")
    print("[8] recipients resolve            OK")

    # Render
    ctx = {
        **fields,
        "self_email": cfg["self_email"],
        "self_display_name": cfg.get("self_display_name", ""),
        "contact_email": cfg.get("contact_email", ""),
        "contact_phone": cfg.get("contact_phone", ""),
    }
    subject_tpl = cfg["reply"].get("subject_override") or subj
    if not re.match(r"^\s*re:", subject_tpl, re.IGNORECASE):
        subject_tpl = "Re: " + subject_tpl
    subject = render_subject(subject_tpl, ctx)
    html_out = render(HERE / cfg["reply"]["template_path"], ctx)

    print()
    print("==== REPLY ====")
    print(f"To:      {to_hdr}")
    print(f"Cc:      {cc_hdr}")
    print(f"Subject: {subject}")
    print("--- body ---")
    print(html_out)
    print("--- end body ---")
    print()

    if args.dry_run:
        print("[--dry-run] No mail sent. No label applied. No tracker row logged.")
        return 0

    # Real send
    last_msgid = gm.header(thread["messages"][-1], "Message-ID")
    thread_param = thread_id if cfg["reply"].get("reply_in_thread", True) else None
    result = client.send_reply(
        to=to_hdr, cc=cc_hdr, subject=subject, html_body=html_out,
        thread_id=thread_param, in_reply_to=last_msgid or None,
    )
    print(f"[send]  Gmail returned id={result.get('id')} thread={result.get('threadId')}")

    label_id = client.ensure_label(cfg["auto_replied_label"])
    client.add_label_to_thread(thread_id, label_id)
    print(f"[label] applied {cfg['auto_replied_label']!r} to thread {thread_id}")

    for mid in thread_msg_ids:
        try:
            client.mark_read(mid)
        except Exception as e:
            print(f"[mark-read err] {mid}: {e!r}")
    print(f"[mark-read] {len(thread_msg_ids)} message(s) marked read")

    # Tracker log
    orig_to = split_addrs(gm.header(first, "To"))
    dom = orig_to[0].split("@", 1)[1].lower() if orig_to else ""
    block = lookup_merchant_contacts(
        cfg, sheet_lookup,
        domain=dom, name=fields.get("merchant_name"),
        orig_to_email=orig_to[0] if orig_to else "",
    )
    row = _build_log_row(cfg, fields, block)
    log_resp = sc.append_log_row(client.creds, cfg["log_sheet"], row)
    print(f"[log] appended to tab {log_resp['tab']!r} at {log_resp.get('updatedRange')}")

    print("\n✅ SENT.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
