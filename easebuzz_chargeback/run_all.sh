#!/bin/bash
# Production cron entry-point. Iterates over every config one after the other,
# all under the SAME hourly cron. Add a new flow by just adding its config
# filename to the CONFIGS list below.

set -u
cd "$(dirname "$0")"

# Easebuzz family — same orchestrator (main.py), different configs.
EASEBUZZ_CONFIGS=(
    config.yaml          # L1: Chargeback Raised             → Action Required
    config_l2.yaml       # L2: Chargeback Escalation Level 2 → Urgent Action Required
    config_l3.yaml       # L3: Chargeback Escalation Level 3 → Urgent Action Required (Arbitration / L3 CB)
)

for cfg in "${EASEBUZZ_CONFIGS[@]}"; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ===== easebuzz $cfg ====="
    .venv/bin/python main.py --once --config "$cfg"
done

# PayU — direct mails from PayU-Chargeback@payu.in, all 3 levels.
# Cutoff in config_payu.yaml restricts to mails received on/after 2026-06-08.
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ===== payu ====="
.venv/bin/python main_payu.py --once --config config_payu.yaml

# Worldline — separate orchestrator (HTML table parser + preserve original Cc).
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ===== worldline ====="
.venv/bin/python main_worldline.py --once --config config_worldline.yaml
