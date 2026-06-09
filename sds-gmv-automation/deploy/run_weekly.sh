#!/usr/bin/env bash
# Weekly SDS GMV automation — wrapper that the cron entry calls.
#
# Computes last full ISO week (Mon-Sun), fetches transactions from Trino via
# Metabase, and writes the per-merchant successful sum into the GMV sheet.
#
# Usage:
#   ./run_weekly.sh                     # auto-compute last full ISO week
#   ./run_weekly.sh 2026-03-02 2026-03-08   # explicit week (Mon Sun, both inclusive)

set -eo pipefail

cd "$(dirname "$0")"

# Activate venv
if [ ! -d ".venv" ]; then
    echo "ERROR: .venv missing. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi
source .venv/bin/activate

# Compute window
if [ -n "$1" ] && [ -n "$2" ]; then
    START="$1"
    END="$2"
else
    # Last fully-completed ISO week (Mon-Sun). Robust regardless of day:
    # END = most recent Sunday strictly in the past
    #       (= yesterday if today is Monday; further back on other days)
    # START = END - 6 days (Monday of that week)
    DOW=$(date +%u)  # 1=Mon, ..., 7=Sun
    END=$(date -d "$DOW days ago" +%Y-%m-%d)
    START=$(date -d "$END -6 days" +%Y-%m-%d)
fi

CSV="Ops_SALE_TRANSACTION_${START}_${END}.csv"
LOG="logs/run_$(date +%Y%m%d_%H%M%S).log"
mkdir -p logs

echo "==============================================="
echo "Run started:  $(date)"
echo "Week window:  ${START} → ${END}"
echo "Output CSV:   ${CSV}"
echo "Log file:     ${LOG}"
echo "==============================================="

# Pre-flight: VPN / Metabase reachable
HEALTH=$(curl --max-time 10 -s -o /dev/null -w "%{http_code}" https://internal-stats.gokwik.in/api/health || echo "000")
if [ "$HEALTH" != "200" ]; then
    echo "ABORT: Metabase unreachable (HTTP $HEALTH). VPN down or service offline." | tee -a "$LOG"
    exit 2
fi
echo "Metabase health-check: OK ($HEALTH)" | tee -a "$LOG"

# 1. Fetch from Trino via MBQL
echo "[1/2] Fetching weekly data..." | tee -a "$LOG"
python fetch_production_adaptive.py \
    --start "$START" \
    --end "$END" \
    --out "$CSV" 2>&1 | tee -a "$LOG"

# 2. Aggregate + write to Google Sheet
echo "[2/2] Updating Google Sheet..." | tee -a "$LOG"
python update_gmv_weekly.py "$CSV" 2>&1 | tee -a "$LOG"

echo "==============================================="
echo "Run finished: $(date)"
echo "==============================================="
