# SDS GMV Weekly Automation

Automates the Monday-morning ops chore of building the **Same Day Settlement (SDS) GMV** report:

1. Pull last week's transactions from **Trino** via Metabase's MBQL API.
2. Filter to **Easebuzz, successful**, exclude 6 hard-listed test/internal merchants.
3. Filter to the SDS-enabled merchant whitelist from the **MID Tracker** sheet (excluding any merchant whose *Date of Enabling* is later than the week end).
4. Aggregate `TXN AMT` per merchant.
5. Append the result as a new week column in the **SDS_GMV WEEKLY** Google Sheet — auto-inserting rows for any newly-enabled merchants, carrying forward operational markers (`NA`, `SDS disabled`, `Unlive`) for merchants with no txns, and refreshing the Grand Total formula.

Runs every **Monday at 06:30 IST** via cron on the chargeback EC2 box.

---

## TL;DR 

| Question | Answer |
|---|---|
| Where is it running? | `chargeback` EC2 (`ip-10-10-53-224.ap-south-1`), user `ec2-user`, dir `~/sds-gmv-automation/` |
| When does it fire? | Mondays 06:30 IST (`30 6 * * 1` in `crontab -l`) |
| What does it write? | A new week column on the `SDS_GMV WEEKLY` tab of [this sheet](https://docs.google.com/spreadsheets/d/1UhGckgb4OYauZxJCq4sw3WR2XVWwJ_ew-KkzjdGgndY/edit?gid=0) |
| Where are the logs? | `~/sds-gmv-automation/logs/cron.log` + per-run `logs/run_YYYYMMDD_HHMMSS.log` |
| Who does it auth as? | `chargeback-automation@gokwik.co` (OAuth user, refresh token in `oauth_token.json`) |
| How long does it take? | ~5–6 min fetch + ~10 s sheet write |
| How do I run it manually? | `cd ~/sds-gmv-automation && ./run_weekly.sh` (auto last week) or `./run_weekly.sh 2026-05-04 2026-05-10` (explicit window) |
| Did this Monday's run land? | `tail -100 logs/cron.log` and look for `Run finished: …` |

---

## Architecture

```
                        ┌──────────────────────────────────────┐
                        │ MID Tracker sheet                    │
                        │ (Merchant ID + Name + Date Enabled)  │
                        └──────────────────────────────────────┘
                                       │
                                       │ whitelist + date filter
                                       ▼
┌──────────────────┐   MBQL    ┌───────────────────────────────────┐
│  Trino_Prod      │ ────────▶ │  fetch_production_adaptive.py     │
│ (Starburst)      │ (per-day  │  • day-by-day pull                │
│ via Metabase     │  chunked) │  • adaptive halving on timeout    │
│ internal-stats   │           │  • writes Ops_SALE_TRANSACTION    │
│ .gokwik.in       │           │    _<start>_<end>.csv             │
└──────────────────┘           └───────────────────────────────────┘
                                       │
                                       │ CSV
                                       ▼
                               ┌──────────────────────────────┐
                               │  update_gmv_weekly.py        │
                               │  • aggregate sum by merchant │
                               │  • insert new tracker rows   │
                               │  • carry-forward markers     │
                               │  • append week column        │
                               │  • refresh Grand Total       │
                               └──────────────────────────────┘
                                       │
                                       ▼
                               ┌──────────────────────────────┐
                               │  SDS_GMV WEEKLY (Google      │
                               │  Sheet) — one new column per │
                               │  weekly run                  │
                               └──────────────────────────────┘

        ┌─────────────────────────────────────────────────────────┐
        │  Orchestrator: run_weekly.sh (called by cron Mon 06:30) │
        │  • computes last full ISO week (Mon–Sun)                │
        │  • activates venv                                       │
        │  • pre-flight Metabase health check                     │
        │  • calls fetch_production_adaptive.py                   │
        │  • calls update_gmv_weekly.py                           │
        │  • tees output to logs/                                 │
        └─────────────────────────────────────────────────────────┘
```

---

## Files on the server

Path: `/home/ec2-user/sds-gmv-automation/`

| File | Purpose |
|---|---|
| `run_weekly.sh` | Cron entry-point. Computes the week window, runs the two python scripts in sequence, tees logs. |
| `fetch_production_adaptive.py` | Metabase MBQL fetcher. Day-chunks the window, adaptively halves on proxy timeout, also reads the MID Tracker whitelist + applies the date-of-enabling filter. Writes a CSV. |
| `update_gmv_weekly.py` | Aggregator + sheet writer. Reads the CSV, sums per merchant, inserts missing tracker rows, writes the new column with marker carry-forward. |
| `requirements.txt` | Python deps: `requests`, `python-dotenv`, `openpyxl`, `gspread`, `google-auth`, `google-auth-oauthlib`, `google-api-python-client`. |
| `env` | Metabase URL + API key. **Secret.** `chmod 600`. |
| `oauth_credentials.json` | Google OAuth client config (installed-app type). **Secret.** |
| `oauth_token.json` | Google OAuth access + refresh token for `chargeback-automation@gokwik.co`. **Secret.** Auto-refreshed by the scripts. |
| `credentials.json` | Legacy Google service-account key. **Currently unused** — kept only in case we revert. |
| `logs/` | Per-run logs (`run_YYYYMMDD_HHMMSS.log`) and the cron-level rollup `cron.log`. |
| `.venv/` | Python 3.9 virtualenv. |
| `.parts/` | Per-day partial CSVs, cleared at start of each run. |
| `Ops_SALE_TRANSACTION_<start>_<end>.csv` | Last week's raw fetch. Overwritten each Monday. Kept on disk for debugging. |
| `README.md` | This file. |

---

## Data flow, step by step

### 1. Cron fires (Mon 06:30 IST)

```
30 6 * * 1 /home/ec2-user/sds-gmv-automation/run_weekly.sh >> /home/ec2-user/sds-gmv-automation/logs/cron.log 2>&1
```

### 2. `run_weekly.sh` computes the window

Robust against being run on any weekday — computes the **last fully-completed ISO week (Mon → Sun)**:

```bash
DOW=$(date +%u)                              # 1=Mon … 7=Sun
END=$(date -d "$DOW days ago" +%Y-%m-%d)     # most recent Sunday (in the past)
START=$(date -d "$END -6 days" +%Y-%m-%d)    # Monday of that week
```

Override is supported for backfill: `./run_weekly.sh 2026-05-04 2026-05-10`.

### 3. Pre-flight: Metabase reachability

`curl https://internal-stats.gokwik.in/api/health` — aborts cleanly if non-200 (VPN down / service offline).

### 4. Fetch from Trino — `fetch_production_adaptive.py`

For each of the 7 days in the window:
1. Build an MBQL query that
   - sources from `lakehouse.gk_lakehouse_gold.unified_orders` (date filter pushes down here),
   - inner-joins `lakehouse.gk_lakehouse_views.transactions_model_view`,
   - joins `lakehouse.gk_lakehouse_gold.merchants_master` for the merchant name,
   - filters `payment_provider = 'easebuzz'`, excludes 6 hard-listed merchants `{6792, 3500, 3742, 2928, 13947, 9462}`,
   - restricts to the **MID Tracker whitelist** (excluding rows whose *Date of Enabling* > the week's end),
2. POSTs to `/api/dataset/csv`,
3. Writes the day's response to `.parts/production_<start>_<end>/day_<YYYY-MM-DD>.csv`.

**Adaptive halving:** if a day-sized query times out (Metabase proxy is 60 s), split into halves and retry. Halving cascades: 24h → 12h → 6h → 3h → 90m → 45m → 22m floor. Each successful slice is checkpointed; on retry we resume from the partial state.

The 7 day-parts are concatenated into `Ops_SALE_TRANSACTION_<start>_<end>.csv`.

### 5. MID Tracker fetch

Read from sheet [`1CPuJSbd…`](https://docs.google.com/spreadsheets/d/1CPuJSbd4emdVzfUOpr7cyYE0FBLlqlYE3_OHQtQATcY) tab gid `759157239`, columns:

| A | B | C |
|---|---|---|
| Merchant ID | Merchant Name | Date of Enabling |

Rows where `Date of Enabling > week_end` are excluded — newly-enabled merchants don't pollute past weeks.

Date is parsed permissively (`%d-%B-%Y`, `%d-%b-%Y`, `%d-%m-%Y`, `%Y-%m-%d`, …). Unparseable dates pass through (treated as "always enabled") and a warning is logged.

### 6. Aggregate + write — `update_gmv_weekly.py`

Auth: OAuth user `chargeback-automation@gokwik.co` (token in `oauth_token.json`, auto-refreshed and persisted back to disk).

Steps:

1. **Read** the GMV tab (`SDS_GMV WEEKLY`, gid 0) and the MID Tracker.
2. **Insert missing tracker rows** — any merchant in the tracker (filtered by *Date of Enabling*) that isn't in the GMV sheet gets a new row inserted *above* the Grand Total row, with `NA` in every past-week column. Uses `insertDimension` + `inheritFromBefore=True` so formatting/colour matches the row above.
3. **Aggregate** the CSV: sum `TXN AMT` per merchant where status indicates success (matches the existing column semantics).
4. **Build the new column** — for each existing GMV row:
   - If the merchant has a sum, write the rounded number.
   - Otherwise, look back through earlier week columns (skipping Sheets error strings like `#VALUE!`) for the most recent **non-numeric** value — `NA` / `SDS disabled` / `Unlive` etc. Carry that forward.
   - Otherwise leave blank.
5. **Grand Total row** gets a fresh `=SUM(<col>2:<col><last_data_row>)`.
6. **Expand the grid** if the new column lands past the current `col_count` (sheets cap at the per-tab grid edge). Call `add_cols(...)` first.
7. **Write** the column in a single `values.update` with `valueInputOption="USER_ENTERED"`.
8. **Copy paste-format** from the previous week column so colours stay consistent.

The script prints a one-line summary at the end:

```
New column index:    98 (CT)
Rows with TXN sum:   120
Rows carried marker: 53
Rows left blank:     0
SDS merchants not in GMV (skipped): 0
```

### 7. Logging

Two log files per run:

- `logs/run_YYYYMMDD_HHMMSS.log` — detailed (one per invocation of `run_weekly.sh`)
- `logs/cron.log` — appended every cron firing; convenient for `tail`

Both contain the same stdout/stderr (tee'd).

---

## Authentication

### Metabase API key (in `env`)

```ini
METABASE_URL=https://internal-stats.gokwik.in
METABASE_API_KEY=mb_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

`chmod 600`. The key is a personal API key — if the owner leaves or rotates it, the pipeline silently breaks. **Long-term:** migrate to a Metabase service account. (See *Sharp edges* below.)

### Google OAuth (in `oauth_credentials.json` + `oauth_token.json`)

OAuth client is an **installed-app** type (loopback redirect). Authorizes as **`chargeback-automation@gokwik.co`** — that account must be **Editor** on both:

- The GMV sheet (`1UhGckgb4OYauZxJCq4sw3WR2XVWwJ_ew-KkzjdGgndY`)
- The MID Tracker sheet (`1CPuJSbd4emdVzfUOpr7cyYE0FBLlqlYE3_OHQtQATcY`)

The token file stores a refresh token; both scripts refresh and persist the new access token transparently.

**Refresh tokens can be revoked** (Google account password change, manual revoke from Security settings, or — rarely — long-unused expiry). When that happens you'll see:

```
google.auth.exceptions.RefreshError: ('invalid_grant: Token has been expired or revoked.', ...)
```

Recovery — see *Rotating the OAuth token* below.

---

## Cron entry (do NOT remove)

```cron
30 6 * * 1 /home/ec2-user/sds-gmv-automation/run_weekly.sh >> /home/ec2-user/sds-gmv-automation/logs/cron.log 2>&1
```

Shares the crontab with `chargeback-automation`. If you redeploy that service, **append** our line — don't replace the whole crontab. Verify after:

```bash
crontab -l | grep sds-gmv
```

If our line is missing, re-install (idempotent, append-only):

```bash
( crontab -l 2>/dev/null | grep -v "sds-gmv-automation/run_weekly.sh"; \
  echo "30 6 * * 1 /home/ec2-user/sds-gmv-automation/run_weekly.sh >> /home/ec2-user/sds-gmv-automation/logs/cron.log 2>&1" \
) | crontab -
```

---

## How to run manually

### Last full ISO week (what cron does)

```bash
cd ~/sds-gmv-automation
./run_weekly.sh
```

### Backfill or specific window

```bash
./run_weekly.sh 2026-05-04 2026-05-10        # Mon–Sun, both inclusive
```

### Just the sheet write (CSV already on disk)

```bash
cd ~/sds-gmv-automation
source .venv/bin/activate
python update_gmv_weekly.py Ops_SALE_TRANSACTION_2026-05-11_2026-05-17.csv
```

### Dry-run the sheet write

```bash
python update_gmv_weekly.py Ops_SALE_TRANSACTION_2026-05-11_2026-05-17.csv --dry-run
```

Prints the would-write column without touching the sheet.

---

## Failure modes & runbook

### a) Cron didn't fire / crontab missing

Check `crontab -l | grep sds-gmv`. If absent, re-install (see *Cron entry* above) — most likely a redeploy of the neighboring `chargeback-automation` service replaced the crontab.

### b) Metabase 502 / 504 / VPN down

The pre-flight `curl /api/health` aborts cleanly with `ABORT: Metabase unreachable`. Re-run after the network/Metabase is back. No retries are scheduled — Monday's run sticks.

### c) OAuth `invalid_grant: Token has been expired or revoked`

The refresh token is dead. Rotate per *Rotating the OAuth token* below.

### d) `Range … exceeds grid limits`

The script auto-expands the grid before writing, so this shouldn't recur. If it does, manually expand:

```python
ws.add_cols(1)      # or however many needed
ws.add_rows(50)
```

### e) `#VALUE!` cells in the new column

The script ignores Sheets error strings (cells starting with `#`) when carrying markers forward — so this shouldn't happen for new columns. If you see it in an *old* column, it means a formula in that column references a text cell. Repair manually.

### f) Trino temporarily slow — adaptive halving still fails at 22m floor

Means Trino infra has a real problem. Wait it out, then re-run `./run_weekly.sh <start> <end>` for the same window. The script wipes `.parts/*` at start so it won't reuse stale partial data.

### g) New merchants showed up but didn't get inserted

Verify the tracker sheet's *Date of Enabling* — it must be **≤ week_end** for that merchant to be eligible. Otherwise they'll be added next week instead.

---

## Rotating the OAuth token

When the refresh token gets revoked, mint a new one. Requires a one-time browser sign-in.

**On a machine with a browser** (typically your laptop, since the EC2 doesn't have one):

```bash
# 1. Copy oauth_credentials.json from the server
scp chargeback:~/sds-gmv-automation/oauth_credentials.json /tmp/

# 2. Run the OAuth flow
python3 - <<'PY'
from google_auth_oauthlib.flow import InstalledAppFlow
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
flow = InstalledAppFlow.from_client_secrets_file("/tmp/oauth_credentials.json", SCOPES)
creds = flow.run_local_server(port=0, open_browser=True, prompt='consent')
open("oauth_token.json", "w").write(creds.to_json())
print("new token written")
PY

# 3. A browser tab opens — sign in as chargeback-automation@gokwik.co and grant access.

# 4. Push the new token back to the server
scp oauth_token.json chargeback:~/sds-gmv-automation/oauth_token.json
ssh chargeback 'chmod 600 ~/sds-gmv-automation/oauth_token.json'
```

Test:

```bash
ssh chargeback 'cd ~/sds-gmv-automation && source .venv/bin/activate && python -c "
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
c = Credentials.from_authorized_user_file(\"oauth_token.json\", [\"https://www.googleapis.com/auth/spreadsheets\"])
c.refresh(Request())
print(\"OK, expires\", c.expiry)
"'
```

---

## Configuration reference

### Hard-coded constants worth knowing

| Constant | Where | Value |
|---|---|---|
| `EXCLUDED_MERCHANTS` | `fetch_production_adaptive.py` + `update_gmv_weekly.py` | `{6792, 3500, 3742, 2928, 13947, 9462}` (test/internal merchants) |
| `payment_provider` filter | `fetch_production_adaptive.py` | `'easebuzz'` |
| Target sheet ID (GMV) | `update_gmv_weekly.py` (`DEFAULT_SPREADSHEET_ID`) | `1UhGckgb4OYauZxJCq4sw3WR2XVWwJ_ew-KkzjdGgndY` |
| Target tab gid (GMV) | `update_gmv_weekly.py` (`DEFAULT_GID`) | `0` (`SDS_GMV WEEKLY`) |
| MID Tracker sheet ID | `fetch_production_adaptive.py` | `1CPuJSbd4emdVzfUOpr7cyYE0FBLlqlYE3_OHQtQATcY` |
| MID Tracker gid | `fetch_production_adaptive.py` | `759157239` |
| Adaptive halving floor | `fetch_production_adaptive.py` | 22 minutes |
| Metabase DB id | discovered at runtime | `db='Trino_Prod' id=30` |

### Trino tables actually queried

| Logical | Catalog.schema.table |
|---|---|
| Order timestamps (source of date filter) | `lakehouse.gk_lakehouse_gold.unified_orders` |
| Transaction-level data (joined) | `lakehouse.gk_lakehouse_views.transactions_model_view` |
| Merchant name lookup | `lakehouse.gk_lakehouse_gold.merchants_master` |

---

## Sharp edges to be aware of

1. **Crontab is shared** with chargeback-automation. Always edit append-only — see the install snippet above. The cron line has already been wiped once (May 5 → May 12) by a chargeback-automation redeploy.

2. **The `Manual` and `Difference` columns** (cols 95–96) were added by ops between our automation column and the rightmost edge. The Difference column has formula `=CP-CQ`, which produces `#VALUE!` for any row whose previous-week value is text (`NA` etc.). The script skips error strings during marker carry-forward — but if anyone *moves* these columns or adds more formula columns, re-verify the carry-forward logic still picks the right "previous value."

3. **Grand Total formula in older columns** is `=SUM(<col>2:<col>158)` — it does **not** include rows 159+ that were auto-inserted later by the automation. Past totals don't reflect new merchants' (zero) txns. If ops cares, the historical SUM ranges need a one-time fix.

4. **Personal Metabase API key** — if the owner of the key leaves GoKwik, fetches will silently 401. Rotate to a service-account key when one's available.

5. **OAuth user-token model** — bus factor is the `chargeback-automation@gokwik.co` account's password and 2FA. Treat it like service infrastructure.

6. **No alerting** on cron failure. `cron.log` accumulates silently. If you want Slack/email notifications, add a tail-and-grep step at the end of `run_weekly.sh` (e.g. detect "Traceback" in this run's log and POST to a Slack webhook).

7. **Python 3.9 EOL** — the venv on the server is Py 3.9, which Google has put on best-effort-only support. Works fine today; deprecation warnings are noisy but harmless. Upgrade to 3.11+ when convenient.

---

## Incident history

| Date | Symptom | Root cause | Fix |
|---|---|---|---|
| 2026-05-04 | Wrong sheet ID written to | Old service-account creds + old sheet ID `1Nv_…` were hard-coded; ops actually use `1UhGckgb…` | Switched defaults, regenerated OAuth as user account |
| 2026-05-04 | 12 newly-enabled (4 May) merchants leaked into the 27 Apr – 3 May column | Whitelist wasn't filtering on *Date of Enabling* | Added `_parse_enable_date()` + `week_end` filter in tracker read |
| 2026-05-04 | New auto-inserted rows had white (no) formatting | Used `insert_rows`, which doesn't inherit format | Switched to `insertDimension` + `inheritFromBefore=True` |
| 2026-05-04 | `EXCLUDED_MERCHANTS` short-circuit was erasing `SDS disabled` markers | Code path returned `""` before the marker-carry-forward block | Removed the short-circuit; excluded merchants now fall through to carry-forward |
| 2026-05-12 | Cron entry vanished between May 5 and May 12 | Chargeback-automation redeploy wholesale-replaced crontab | Re-installed append-only; documented in this README |
| 2026-05-12 | OAuth refresh token revoked | Cause unknown (possibly a Google-side revoke or password change on the chargeback-automation account) | Re-ran OAuth flow locally, copied fresh `oauth_token.json` back |
| 2026-05-12 | Inserted column had `#VALUE!` in marker rows | Carry-forward picked the value from the `Difference` column (a formula column that evaluates to `#VALUE!` when its inputs are text) | Patched `_latest_non_numeric` to skip cells starting with `#` |
| 2026-05-18 | `Range 'CT1:CT177' exceeds grid limits. Max columns: 97` | Sheet's per-tab `col_count` was exactly 97; we tried to write col 98 | Added `add_cols(new_col - col_count)` before the update call |

---

## Re-deploying after a code change

```bash
# 1. Edit locally

# 2. Push the changed file
scp fetch_production_adaptive.py chargeback:~/sds-gmv-automation/
scp update_gmv_weekly.py        chargeback:~/sds-gmv-automation/
scp deploy/run_weekly.sh        chargeback:~/sds-gmv-automation/run_weekly.sh

# 3. Sanity-check
ssh chargeback 'cd ~/sds-gmv-automation && bash -n run_weekly.sh && \
  source .venv/bin/activate && \
  python -c "import fetch_production_adaptive, update_gmv_weekly; print(\"imports OK\")"'

# 4. Optional dry-run with last week's CSV
ssh chargeback 'cd ~/sds-gmv-automation && source .venv/bin/activate && \
  python update_gmv_weekly.py Ops_SALE_TRANSACTION_2026-05-11_2026-05-17.csv --dry-run'
```

---

## Contacts

- **Automation owner:** Eeshu Yadav (`eeshu.yadav@gokwik.co`)
- **Google account:** `chargeback-automation@gokwik.co`
