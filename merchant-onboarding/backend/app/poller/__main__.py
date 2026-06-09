"""Run the poller from the CLI:
    python -m app.poller                     # daily sync (Submerchant + seed + Slack)
    python -m app.poller --backfill          # one-time import of the Easebuzz tab
    python -m app.poller --refetch-kickstarts  # hourly — re-hit Kickoff API for blank kickstarts
"""
import sys

from app.poller.poll import main

if __name__ == "__main__":
    sys.exit(main())
