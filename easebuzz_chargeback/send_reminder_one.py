"""One-off: send a single reminder reply on a specific thread (by transaction
id substring) using the L1/L2/L3 config and templates/reminder.html.j2.

Use ONLY for manual testing — production reminders flow through reminders.run_reminders.

Usage:
    python3 send_reminder_one.py --tx E2604070WM4RSO --config config.yaml
    python3 send_reminder_one.py --tx E2603030UPOXUB --config config_l2.yaml --dry-run
"""
from __future__ import annotations

import argparse
import sys
from email.utils import getaddresses
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

import gmail_client as gm

HERE = Path(__file__).parent


def split_addrs(raw: str) -> list[str]:
    return [a for _n, a in getaddresses([raw or ""]) if a]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tx",     required=True, help="transaction id, e.g. E2604070WM4RSO")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load((HERE / args.config).read_text())
    client = gm.make_client(cfg, base_dir=HERE)

    # Find the thread by subject containing the transaction id.
    matches = client.search(f'subject:{args.tx}')
    if not matches:
        print(f"no thread found for {args.tx!r}", file=sys.stderr)
        return 1

    full = client.get_message(matches[0]["id"])
    tid  = full["threadId"]
    thread = client.get_thread(tid)
    msgs = thread.get("messages", [])
    print(f"thread {tid}: {len(msgs)} messages")

    self_l = cfg["self_email"].lower()
    external_count = sum(
        1 for m in msgs
        if self_l not in [a.lower() for a in split_addrs(gm.header(m, "From"))]
    )
    if external_count != 1:
        print(f"  REFUSED: thread has {external_count} external message(s) "
              f"(someone replied) — reminder logic forbids this",
              file=sys.stderr)
        return 2

    # Find OUR auto-reply in the thread.
    self_l = cfg["self_email"].lower()
    ours = None
    for m in msgs:
        from_addrs = [a.lower() for a in split_addrs(gm.header(m, "From"))]
        if self_l in from_addrs:
            ours = m
    if not ours:
        print(f"  REFUSED: no message from {cfg['self_email']!r} in thread",
              file=sys.stderr)
        return 3

    # Build the reminder.
    to_hdr = gm.header(ours, "To")
    cc_hdr = gm.header(ours, "Cc")
    original_subject = gm.header(msgs[0], "Subject")
    subject = (original_subject if original_subject.lower().startswith("re:")
               else f"Re: {original_subject}")
    last_msgid = gm.header(msgs[-1], "Message-ID")

    template_rel = (cfg.get("reminder") or {}).get(
        "template_path", "templates/reminder.html.j2"
    )
    env = Environment(
        loader=FileSystemLoader(str(HERE)),
        autoescape=select_autoescape(["html"]),
    )
    body_html = env.get_template(template_rel).render({})

    print(f"  to:      {to_hdr}")
    print(f"  cc:      {cc_hdr}")
    print(f"  subject: {subject}")

    if args.dry_run:
        print("  [DRY-RUN] not sending")
        return 0

    client.send_reply(
        to=to_hdr,
        cc=cc_hdr,
        subject=subject,
        html_body=body_html,
        thread_id=tid,
        in_reply_to=last_msgid or None,
    )
    label = client.ensure_label(cfg["auto_reminded_label"])
    client.add_label_to_thread(tid, label)
    print(f"  [SENT] reminder, applied label {cfg['auto_reminded_label']!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
