# Merchant Onboarding Dashboard

Replaces the Easebuzz tab of the "Ops Updates - Merchant Onboarding" spreadsheet
with a dedicated dashboard, plus an hourly poller that catches new merchants
added to the Gokwik Submerchant list.

## Components

```
ops_infra/3/
├── backend/
│   ├── app/
│   │   ├── main.py + routers/ ...   FastAPI on :8001
│   │   └── poller/                  Google-Sheets → Postgres sync
│   ├── scripts/
│   │   ├── init_db.py               apply schema.sql
│   │   └── run_poller.sh            cron entry
│   ├── requirements.txt
│   └── .env.example
└── frontend/                        Vite + React + Tailwind + shadcn
```

Both pieces use **one local Postgres database** (`merchant_onboarding`) — no
Docker. The poller writes; the FastAPI reads + accepts dashboard edits;
edited rows are protected from being clobbered by the next sync.

## End-to-end setup

```bash
# 1. Postgres
createdb merchant_onboarding

# 2. Backend
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                   # adjust DB url if needed
python scripts/init_db.py              # apply schema (idempotent)
python -m app.poller                   # first sync — fills the DB
./run.sh &                             # FastAPI on :8001

# 3. Frontend  (in a new terminal)
cd ../frontend
npm install
npm run dev                            # http://localhost:5173

# 4. Cron the poller
crontab -e
0 * * * * /home/eeshu/Desktop/ops_infra/3/backend/scripts/run_poller.sh >> /home/eeshu/Desktop/ops_infra/3/backend/logs/poll.log 2>&1
```

## Source sheets

| Tab | Sheet ID | gid | Role |
|---|---|---|---|
| `Merchant Onboarding` | `1-Mj_dTa1LTzyB2ucidNhDqQqNsS09rEC742t9t61cgk` | 335949376 | new-merchant detection |
| `Easebuzz`            | `1X5e3r_0hz4oAf_6qu6mIrFEILco8pss-WmxlbVN_jio` | 0         | onboarding pipeline (replaced by dashboard) |

Both must be shared with **`chargeback-automation@gokwik.co`** — the OAuth account behind `ops_infra/1/token.json`.

## How edits behave

Dashboard edits set `easebuzz_onboarding.source = 'dashboard'`. The hourly
poller honors that: it still refreshes most fields from the sheet, but
**won't overwrite** `onboarding_status`, `remarks`, `ops_remarks`, or
`delivery` on dashboard-edited rows.
