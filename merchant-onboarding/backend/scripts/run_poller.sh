#!/usr/bin/env bash
# Cron entry for the weekly merchant-onboarding sync.
#
# The Gokwik Submerchant list is updated weekly, so weekly is the right
# cadence. Run Monday 9am IST after the previous week's intake settles.
#
# Crontab:
#   0 9 * * 1 /home/eeshu/Desktop/ops_infra/3/backend/scripts/run_poller.sh \
#       >> /home/eeshu/Desktop/ops_infra/3/backend/logs/poll.log 2>&1
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

python -m app.poller "$@"
