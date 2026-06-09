#!/usr/bin/env bash
# Cron entry for the hourly kickstart-refetch job.
#
# The daily run_poller.sh at 04:00 IST only calls the Kickoff API for
# brand-new merchants at seed time. Seeded rows whose kickstart was blank
# at seed time would otherwise sit blank forever. This script runs every
# hour and re-hits the Kickoff API just for those rows, so a kickoff date
# that gets entered into Mintdash mid-day is reflected on the dashboard
# within the next hour (without re-pulling the full Submerchant tab or
# Slack channel — both of which the daily job already handled).
#
# Crontab:
#   0 * * * * /home/ec2-user/ops-merchant-onboarding/backend/scripts/refetch_kickstarts.sh \
#       >> /home/ec2-user/ops-merchant-onboarding/backend/logs/refetch_kickstarts.log 2>&1
set -euo pipefail

BACKEND="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BACKEND"

mkdir -p logs

if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

if [[ -d .venv ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

python -m app.poller --refetch-kickstarts "$@"
