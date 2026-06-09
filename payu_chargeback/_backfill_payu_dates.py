"""LOCAL-ONLY one-shot: backfill PayU auto-replies for a specific date range.

Cron's PayU after: cutoff is 2026/06/08 — anything received before that is
permanently excluded. This script lets you target an earlier date window
manually (e.g. June 5/6/7) without touching the deployed config.

Reuses main_payu.process_thread end-to-end so behavior matches cron exactly:
   - URL extract / attachment parse (all 4 sheets) / merchant resolution
   - Recipient = sheet email OR forwarded-To fallback OR mail's To header
   - Body = templates/reply_payu.html.j2 with inline cases list
   - Attachment = chargeback.xls passed through
   - Apply auto-replied-payu label + tracker rows (one per case)

Usage:
    python3 _backfill_payu_dates.py --after 2026/06/05 --before 2026/06/08 --dry-run
    python3 _backfill_payu_dates.py --after 2026/06/05 --before 2026/06/08 --send
"""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path

import yaml

import gmail_client as gm
import merchant_onboarding as mo
from logging_setup import log, setup_logging
from main_payu import process_thread
from planhat_client import PlanhatClient


def _build_planhat(cfg):
    ph = cfg.get("planhat") or {}
    if not ph.get("token_path"):
        return None
    try:
        return PlanhatClient(
            token_path=str(HERE / ph["token_path"]),
            base_url=ph.get("base_url", "https://api.planhat.com"),
        )
    except Exception:
        return None

HERE = Path(__file__).parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--after",  required=True, help="Gmail-style YYYY/MM/DD (inclusive)")
    ap.add_argument("--before", required=True, help="Gmail-style YYYY/MM/DD (exclusive)")
    ap.add_argument("--config", default="config_payu.yaml")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--send",    action="store_true")
    args = ap.parse_args()

    setup_logging(log_dir=HERE / "logs")
    cfg = yaml.safe_load((HERE / args.config).read_text())

    # Override the query window: this run only looks at args.after..args.before
    cfg["extra_query"] = re.sub(r"after:\S+", f"after:{args.after}", cfg["extra_query"])
    if "before:" not in cfg["extra_query"]:
        cfg["extra_query"] += f" before:{args.before}"
    else:
        cfg["extra_query"] = re.sub(r"before:\S+", f"before:{args.before}", cfg["extra_query"])
    print(f"[backfill] query window: after:{args.after} before:{args.before}  dry_run={args.dry_run}")
    print(f"[backfill] extra_query : {cfg['extra_query']!r}")

    client = gm.make_client(cfg, base_dir=HERE)
    merchant_lookup = mo.build_lookup(client.creds, cfg["merchant_sheet"])
    print(f"[onboarding] loaded {len(merchant_lookup)} merchant URL row(s)")
    planhat = _build_planhat(cfg)
    label_replied = None if args.dry_run else client.ensure_label(cfg["auto_replied_label"])
    label_manual  = None if args.dry_run else client.ensure_label(cfg["auto_logged_manual_label"])

    query = cfg["extra_query"]
    matches = client.search(query)
    print(f"[backfill] {len(matches)} trigger message(s) returned")

    seen: set[str] = set()
    counts = {"processed": 0, "errored": 0}
    for m in matches:
        tid = m.get("threadId")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        try:
            process_thread(
                client, tid, cfg, label_replied, label_manual, merchant_lookup,
                planhat=planhat, dry_run=args.dry_run,
            )
            counts["processed"] += 1
        except Exception:
            log.exception("thread %s failed", tid)
            counts["errored"] += 1

    print(f"\n[backfill] summary: unique_threads={len(seen)}  "
          f"processed={counts['processed']}  errored={counts['errored']}  "
          f"dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
