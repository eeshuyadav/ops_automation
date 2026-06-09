# Merchant Onboarding — Backend

FastAPI service + weekly Google-Sheets poller, both in one Python package.

```
backend/
├── app/
│   ├── main.py          FastAPI app (CORS + API-key gate + stale-run reaper)
│   ├── config.py        env-driven settings
│   ├── dependencies.py  shared FastAPI deps (API-key check)
│   ├── db.py            async SQLAlchemy engine + session
│   ├── models.py        ORM (mirrors poller/schema.sql)
│   ├── schemas.py       Pydantic IO
│   ├── adapters.py      ORM → Pydantic
│   ├── routers/
│   │   ├── merchants.py
│   │   ├── easebuzz.py  list + GET + PATCH (dashboard edits)
│   │   └── sync.py      GET sync-runs audit log + health
│   └── poller/
│       ├── schema.sql   DDL — single source of truth
│       ├── poll.py      run_sync() + run_backfill() + main()
│       ├── sheets_io.py Google Sheets fetchers
│       ├── gmail_client.py  vendored OAuth helper (shared scopes with ops_infra/1)
│       ├── normalize.py merchant-name normalizer
│       ├── __init__.py
│       └── __main__.py  `python -m app.poller`
├── scripts/
│   ├── init_db.py       applies schema.sql (idempotent)
│   └── run_poller.sh    cron entry
├── requirements.txt
├── .env.example
└── run.sh               FastAPI dev server on :8001
```

## Setup

```bash
cd /home/eeshu/Desktop/ops_infra/3/backend

# 1. Postgres database
createdb merchant_onboarding             # or use psql -c 'CREATE DATABASE ...'

# 2. Python deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Config
cp .env.example .env
$EDITOR .env                             # set DATABASE_URL / SYNC_DATABASE_URL

# 4. Apply schema
python scripts/init_db.py

# 5. First sync
python -m app.poller                     # or:  scripts/run_poller.sh

# 6. Dev server
./run.sh                                 # FastAPI on http://localhost:8001
```

## Auth

Every endpoint **except `/api/health`** requires the request to carry the
shared secret in the `X-API-Key` header:

```bash
curl -H "X-API-Key: $API_KEY" http://localhost:8001/api/merchants
```

The secret comes from the `API_KEY` env var (see `.env.example`). When
`API_KEY` is empty, the check is a no-op and the server logs a warning
at startup — convenient for local dev, **never** acceptable in production.
The comparison uses `hmac.compare_digest` so timing leaks aren't a risk.

`/api/health` stays unauthenticated so external uptime monitors can probe
liveness without a secret. The richer `/api/sync/health` endpoint *is*
gated and returns 503 when the last poller run failed or is older than
8 days — wire it to your alerting once monitors have the API key.

## API

| Method | Path | Purpose |
|---|---|---|
| GET    | `/api/health` | liveness (unauthenticated) |
| GET    | `/api/merchants` | list (q, limit, offset) |
| GET    | `/api/merchants/new-unlinked` | merchants with no Easebuzz row yet |
| GET    | `/api/merchants/{mid}` | one merchant |
| GET    | `/api/easebuzz` | list (q, status, delayed, days, limit, offset) |
| GET    | `/api/easebuzz/stats` | counts grouped by onboarding_status |
| GET    | `/api/easebuzz/eb-times` | per-merchant EB SLA snapshot |
| GET    | `/api/easebuzz/eb-times/analytics` | rich analytics bundle |
| GET    | `/api/easebuzz/timeseries` | daily kickoff/approved volume |
| GET    | `/api/easebuzz/{id}` | one row |
| PATCH  | `/api/easebuzz/{id}` | edit (marks `source='dashboard'`) |
| GET    | `/api/sync/last` | most recent run (with `is_stale`) |
| GET    | `/api/sync/recent?limit=N` | last N runs (N ≤ 100, default 20) |
| GET    | `/api/sync/health` | 200 if last run is fresh + successful, else 503 |

Sync triggering is **out-of-band only** — run `python -m app.poller`
(or the cron entry below). There is no POST endpoint that fires a sync
from the dashboard.

## Cron

Submerchant list is updated weekly, so weekly is the right cadence:

```cron
# Every Monday 9am — pulls Submerchant only and seeds new onboarding rows
0 9 * * 1 /home/eeshu/Desktop/ops_infra/3/backend/scripts/run_poller.sh >> /home/eeshu/Desktop/ops_infra/3/backend/logs/poll.log 2>&1
```

## One-time backfill

Run this exactly ONCE after `init_db.py`:

```bash
cd /home/eeshu/Desktop/ops_infra/3/backend
source .venv/bin/activate
python -m app.poller --backfill
```

This pulls the full Easebuzz tab (~8000 rows) into Postgres + seeds any
post-cutoff Submerchant MIDs that don't have an onboarding row yet.
After this, the Easebuzz Google Sheet is treated as archived; the regular
cron only reads the Submerchant list.

**Backfill refuses to run if any `easebuzz_onboarding` row already has
`source='dashboard'`** — otherwise a second backfill would overwrite manual
edits via the upsert path. Pass `--force` only after auditing the impact:

```bash
python -m app.poller --backfill --force
```

## Seeding rules

The regular cron creates an `easebuzz_onboarding` row for every
Submerchant MID where ALL of these hold:

1. There is no existing onboarding row for this merchant (matched by
   normalized name).
2. Gokwik KYC complete date (col E) is non-blank.
3. That date parses successfully and is **strictly after the seed
   cutoff** (default `2026-05-05`, configurable via `KYC_SEED_CUTOFF`).
   Anything on/before the cutoff is assumed already imported from
   the Easebuzz tab during backfill.

Seeded row gets:
- `docs_received_date` = `kyc_completed_by_ops` = `date_email_sent_to_eb`
   = Gokwik KYC complete date (raw text from the sheet)
- `kickstart_date`, `salt_key_receipt` = from `api_client.py` — STUBBED.
   Replace with real Easebuzz/Gokwik API call when endpoint + auth land.
- `source` = `'seeded'` — dashboard shows a yellow "Needs review" badge
   and sorts these rows to the top.

## Source sheets

| Tab | Sheet ID | gid | Primary key |
|---|---|---|---|
| `Merchant Onboarding` | `1-Mj_dTa1LTzyB2ucidNhDqQqNsS09rEC742t9t61cgk` | `335949376` | MID |
| `Easebuzz` | `1X5e3r_0hz4oAf_6qu6mIrFEILco8pss-WmxlbVN_jio` | `0` | Merchant Name |

Both must be shared with **`chargeback-automation@gokwik.co`** (the OAuth account behind `ops_infra/1/token.json`).

## Dashboard precedence

The poller respects fields the user edited in the UI: once a row has
`source='dashboard'`, the next sync will *not* overwrite
`onboarding_status`, `remarks`, `ops_remarks`, or `delivery` on that row.
Other fields still refresh from the sheet.
