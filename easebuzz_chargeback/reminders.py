"""Send a single +N-hour reminder reply to threads we've auto-replied to,
ONLY if no one else has replied since our send.

Trigger condition (per-thread):
    1. Thread has the `auto_replied_label` (we sent the auto-reply)
    2. Thread does NOT have the `auto_reminded_label` (we haven't reminded yet)
    3. Thread has exactly 2 messages (original + our auto-reply, no human follow-up)
    4. Hours since OUR auto-reply >= cfg["reminder_delay_hours"] (default 48)

When all 4 hold:
    - Reply in the same thread, same TO/CC as our original auto-reply
    - Subject = original "Re: <subject>"
    - Body = templates/reminder.html.j2 (shared across L1/L2/L3)
    - Apply `auto_reminded_label` so we never re-fire on this thread

If any human (merchant, CSM, internal) replies BEFORE the reminder fires, the
thread message count > 2, the trigger condition fails, and we never remind —
permanently. (We do NOT mark such threads as "reminded"; they just remain
without the reminder label, which is correct: nothing to remind about.)
"""
from __future__ import annotations

import datetime
from email.utils import getaddresses
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

import gmail_client as gm
from logging_setup import log


def _split(raw: str) -> list[str]:
    return [a for _n, a in getaddresses([raw or ""]) if a]


def _our_message(thread: dict, self_email: str) -> dict | None:
    """Find the latest message in `thread` whose From contains `self_email`."""
    self_l = self_email.lower()
    found = None
    for msg in thread.get("messages", []):
        from_addrs = [a.lower() for a in _split(gm.header(msg, "From"))]
        if self_l in from_addrs:
            found = msg  # keep walking; we want the LATEST one
    return found


def _internal_date_ms(msg: dict) -> int:
    raw = msg.get("internalDate", "0")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def run_reminders(cfg: dict, base_dir: Path, *, dry_run: bool = False) -> None:
    auto_replied_lbl = cfg.get("auto_replied_label")
    auto_reminded_lbl = cfg.get("auto_reminded_label")
    if not auto_replied_lbl or not auto_reminded_lbl:
        # Reminders aren't configured for this flow — skip silently.
        return

    delay_hours = float(cfg.get("reminder_delay_hours", 48))
    rcfg = cfg.get("reminder") or {}
    template_rel = rcfg.get("template_path", "templates/reminder.html.j2")
    # Hard date floor — never remind on threads where our auto-reply was sent
    # before this date (Gmail format YYYY/MM/DD). Defaults to 2026/05/01 so
    # the reminder feature does not retroactively pester old threads.
    after_date = rcfg.get("after_date", "2026/05/01")

    client = gm.make_client(cfg, base_dir=base_dir)
    label_reminded = (
        None if dry_run else client.ensure_label(auto_reminded_lbl)
    )

    # Restrict scope to recent threads only:
    #   newer_than:14d  – reasonable upper bound (anything older shouldn't
    #                     remind even if no reply landed)
    #   after:<date>    – hard cutoff so older threads (pre-feature-launch)
    #                     never receive a retroactive reminder
    query = (
        f'label:{auto_replied_lbl} -label:{auto_reminded_lbl} '
        f'after:{after_date} newer_than:14d'
    )
    print(f"[reminder] query: {query}  dry_run={dry_run}")
    matches = client.search(query)
    print(f"[reminder] {len(matches)} candidate message(s)")

    env = Environment(
        loader=FileSystemLoader(str(base_dir)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template(template_rel)
    body_html = template.render({})

    self_email = cfg["self_email"]
    now_ms = int(datetime.datetime.now().timestamp() * 1000)
    # Belt-and-braces: also enforce the date floor in code, in case the Gmail
    # `after:` filter ever drifts. Convert "YYYY/MM/DD" -> ms epoch.
    cutoff_ms = int(datetime.datetime.strptime(
        after_date, "%Y/%m/%d"
    ).timestamp() * 1000)

    seen: set[str] = set()
    sent = 0
    skip_replied = 0
    skip_too_early = 0

    for m in matches:
        full = client.get_message(m["id"])
        tid = full.get("threadId")
        if not tid or tid in seen:
            continue
        seen.add(tid)

        thread = client.get_thread(tid)
        msgs = thread.get("messages", [])
        # Count messages NOT from us. The reminder fires only when nobody
        # external has replied — i.e., the only external message is the
        # original chargeback mail, and every other message in the thread
        # comes from us (auto-reply, prior reminder, easebuzz-cc backfill,
        # etc.). This is more robust than counting raw message length, which
        # gets bumped each time the bot itself appends a thread message.
        self_l = self_email.lower()
        external_count = sum(
            1 for mm in msgs
            if self_l not in [a.lower() for a in _split(gm.header(mm, "From"))]
        )
        if external_count != 1:
            print(f"  [skip] thread {tid}: {external_count} external "
                  f"message(s) (someone replied) — no reminder ever")
            skip_replied += 1
            continue

        ours = _our_message(thread, self_email)
        if not ours:
            # Defensive: label says we replied but we can't find our message.
            # Don't risk sending a reminder.
            print(f"  [skip] thread {tid}: no message from {self_email!r}")
            continue

        our_send_ms = _internal_date_ms(ours)
        if our_send_ms < cutoff_ms:
            print(f"  [skip] thread {tid}: our send predates "
                  f"{after_date} cutoff — never remind")
            continue
        elapsed_h = (now_ms - our_send_ms) / 1000 / 3600
        if elapsed_h < delay_hours:
            print(f"  [skip] thread {tid}: only {elapsed_h:.1f}h since our send "
                  f"(need {delay_hours}h)")
            skip_too_early += 1
            continue

        # Build reply: same TO/CC as our original auto-reply, same subject form.
        to_hdr = gm.header(ours, "To")
        cc_hdr = gm.header(ours, "Cc")
        original_subject = gm.header(msgs[0], "Subject")
        subject = (original_subject if original_subject.lower().startswith("re:")
                   else f"Re: {original_subject}")
        last_msgid = gm.header(msgs[-1], "Message-ID")

        if dry_run:
            print(f"  [DRY-RUN] would remind thread {tid} elapsed={elapsed_h:.1f}h")
            print(f"            to={to_hdr}")
            print(f"            cc={cc_hdr}")
            print(f"            subject={subject!r}")
            continue

        try:
            client.send_reply(
                to=to_hdr,
                cc=cc_hdr,
                subject=subject,
                html_body=body_html,
                thread_id=tid,
                in_reply_to=last_msgid or None,
            )
            client.add_label_to_thread(tid, label_reminded)
            print(f"  [sent] reminder for thread {tid}  elapsed={elapsed_h:.1f}h")
            print(f"         to={to_hdr}")
            sent += 1
        except Exception:
            log.exception("reminder send failed for thread %s", tid)

    print(f"[reminder] done: sent={sent} skipped_already_replied={skip_replied} "
          f"skipped_too_early={skip_too_early}")
