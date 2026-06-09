# Salt & Key from Slack — Integration Handoff

**Status:** code complete, parser tested offline against live data, awaiting end-to-end run on the real DB.
**Date:** 2026-05-22
**Project root:** `/home/eeshu/Desktop/ops_infra/3/`

This doc is a handoff for another Claude session (or another engineer). It explains *what* was built, *why*, and *what's left to do*. Read top-to-bottom — context up front, action items at the bottom.

---

## 1. Project context

The Merchant Onboarding Dashboard (`ops_infra/3/`) replaces the Easebuzz tab of the *Ops Updates – Merchant Onboarding* Google Sheet. Stack:

- **Backend:** FastAPI on `:8001`, Python 3, Postgres (`merchant_onboarding` DB).
- **Frontend:** Vite + React + Tailwind + shadcn on `:5173`.
- **Poller:** `python -m app.poller` runs hourly via cron; pulls the Gokwik Submerchant list from Google Sheets and seeds dashboard rows.
- **Source of truth for edits:** dashboard rows with `source = 'dashboard'` are protected from sheet-side overwrites.

Schema is in `backend/app/poller/schema.sql`. The relevant table is `easebuzz_onboarding`, keyed by `name_normalized`, with `merchant_id → merchants.id` (where `merchants.mid` is the natural key from the Submerchant sheet).

---

## 2. The problem

The dashboard surfaces a column **"Salt & Key receipt date"** (DB column `easebuzz_onboarding.salt_key_receipt`, TEXT). For every merchant seeded after the cutoff `2026-05-05`, this column was always blank — the screenshot from the user showed dozens of "Needs review" rows with `—` in the SALT & KEY column.

**Why blank:** the date doesn't exist anywhere in either Google Sheet. It only lives in a Slack channel where an Easebuzz bot posts a daily digest.

**Goal:** read that channel, extract the date per merchant, fill `salt_key_receipt` in the hourly sync. Don't break existing protection rules.

---

## 3. Discovery — Slack channel and message format

**Channel:** `C0ALVC72T6K` in the **GoKwik** Slack workspace. Public channel.

**Posting pattern:** the bot posts one digest per day, format below (Key/Salt redacted here — *they are live credentials in the real message*):

```
:e-mail: *Easebuzz Key & Salt — April 30, 2026*
Total: *15 merchant(s)*

1. Born16 | MID:273430 | Key:<REDACTED> | Salt:<REDACTED> | Email:admin@born16.com | CC EMI:Enabled
2. Glimmora by groverlights | MID:275396 | Key:<REDACTED> | Salt:<REDACTED> | Email:... | CC EMI:Enabled
...
```

When there's no email for the day, the bot posts a one-liner:

```
:information_source: No Easebuzz Key & Salt email found for May 01, 2026.
```

**Important properties confirmed against the live dump (29 messages since 2026-05-01):**

| Property | Value |
|---|---|
| Batch messages | 21 |
| "No email" notices (skip) | 5 |
| "User joined" system messages (skip) | 3 |
| Total `(mid, date)` records yielded | 279 |
| Unique MIDs | 246 |
| Unique header dates | 16 |
| Unparseable headers | 0 |
| Avg merchants per batch | 13.3 (min 2, max 30) |

**Key insight:** every line includes `MID:<digits>` — the Submerchant table's natural key. We **join on MID, not name**. No fuzzy normalization needed.

**Header date** (e.g. "April 30, 2026") = the date Easebuzz issued the salt&key = what goes into `salt_key_receipt`. The Slack message timestamp itself is ~next-morning and irrelevant for the receipt date.

**Slack-specific quirk:** the literal `&` in the header arrives as `&amp;` because Slack HTML-escapes ampersands in message text. The regex matches `&amp;` exactly.

---

## 4. Architecture decisions and why

### 4.1 Why a Slack bot token, not user-account access

The user initially asked IT for Slack access on the existing `chargeback-automation@gokwik.co` Google service account. We pivoted to a dedicated internal Slack app + bot token for these reasons (this exact rationale was sent to the gokwik admin):

- Service accounts shouldn't be humans in Slack — they occupy seats and show up as ghost members in DMs/channels.
- Tightly coupling automation to a Google account means rotating that account silently breaks Slack auth.
- A user account has overly broad access; a bot token can be scoped to one channel.
- A user-account approach has no audit-trail separation between human and automation.
- Slack admins (rightly) push back on user-account automations.

A bot token is workspace-owned, survives team turnover, and is least-privilege.

### 4.2 Slack app config

Internal app **`merchant-onboarding-reader`** in the gokwik workspace.

**OAuth scopes (Bot Token Scopes):**

| Scope | Used for | Required |
|---|---|---|
| `channels:history` | Read messages in the public channel | Yes |
| `channels:read` | Channel metadata sanity checks | Yes |
| `users:read` | Resolve sender names | Optional |
| ~~`chat:write`~~ | — | **Should be removed.** Read-only dashboard; write scope is a footgun if the token leaks. |
| `groups:history`, `groups:read` | Private channels | Not needed (channel is public). Currently added; harmless but unused. |

**Bot:** invited into `C0ALVC72T6K` via `/invite @merchant-onboarding-reader`.

**Token type:** Bot User OAuth Token (`xoxb-…`), stored in `backend/.env` as `SLACK_BOT_TOKEN`. Auth test confirms: `merchantonboardingrea (U0B630L48P2) in team GoKwik`.

### 4.3 Pull, not push

We use `conversations.history` polling on the hourly cron, not Events API webhooks. Reasons:

- The dashboard cron already runs hourly; the salt&key data isn't latency-sensitive (Easebuzz posts daily).
- No public webhook endpoint to expose, no signing secret to verify, no event subscription to configure.
- Backfill is trivial — just walk history from epoch.

### 4.4 What we store vs. what we ignore

| Field in Slack message | Stored? | Why |
|---|---|---|
| `MID:` | Yes (used as join key) | Natural foreign key into `merchants.mid` |
| Header date | Yes → `salt_key_receipt` (ISO text) | The whole point of this integration |
| Merchant name | No | MID is unambiguous; we already have the name from Submerchant |
| **Key value** | **No, ever** | Live credential. Storing this in the dashboard DB would be a security regression. |
| **Salt value** | **No, ever** | Same. |
| Email, CC EMI flag | No | Already in other sources or not relevant to the dashboard |
| Message permalink | Optional — fetched by the dump CLI, not stored by the poller | Useful for one-off investigation; cron skips it to save API calls |

The parser's contract: it yields `SaltKeyRecord(mid, salt_key_date, salt_key_date_text, permalink, posted_at)`. **It never includes Key or Salt.** This is asserted by a test in `dump_slack_messages.py`-driven offline run.

### 4.5 Edit-protection semantics

The existing dashboard rule: rows with `source = 'dashboard'` won't be overwritten by the sheet sync. The Slack sync respects the same rule — if someone has manually set the salt&key date in the dashboard UI, the Slack value won't clobber it.

### 4.6 Transactional safety

`sync_salt_keys_from_slack(cur)` is called *inside* the `SAVEPOINT upserts` block of `run_sync` and `run_backfill`. If the Slack walk fails midway (network, rate limit, API error), the savepoint rollback unwinds the data writes from the entire run but **keeps** the `sync_runs` audit row so `/api/sync/last` reflects the failure. This matches the existing failure semantics for the sheet sync.

### 4.7 Misconfig handling

If `SLACK_BOT_TOKEN` or `SLACK_SALT_KEY_CHANNEL_ID` is empty, the sync **logs `slack_skipped=1` and returns cleanly**. The cron continues without Slack data. Decision: a missing token must never break the existing sheet sync.

---

## 5. Files added / changed

### Added

- **`backend/app/poller/slack_io.py`** — Parser module.
  - `_HEADER_RE`, `_MID_RE` — regexes for the batch digest.
  - `_parse_header_date()` — strict `%B %d %Y` / `%b %d %Y` parser (no `fuzzy=True`).
  - `_iter_history()` — paginated `conversations.history` walker.
  - `fetch_salt_key_records()` — public generator. Skips no-email notices and system messages. Yields `SaltKeyRecord` dataclasses.
  - `build_slack_client()` — token-aware factory; returns `None` on empty token.

- **`backend/scripts/dump_slack_messages.py`** — One-off CLI used during discovery. Dumps the channel to `backend/logs/slack_dump.json` for offline parser dev. Still useful for future debugging. Run with `python scripts/dump_slack_messages.py --since 2026-05-01`.

- **`ops_infra/3/SALT_KEY_SLACK_INTEGRATION.md`** — this file.

### Changed

- **`backend/requirements.txt`** — added `slack-sdk>=3.27.0`.

- **`backend/.env.example`** — documented three new vars:
  ```
  SLACK_BOT_TOKEN=xoxb-replace-me
  SLACK_SALT_KEY_CHANNEL_ID=C0ALVC72T6K
  SLACK_LOOKBACK_DAYS=14
  ```

- **`backend/app/config.py`** — added `slack_bot_token`, `slack_salt_key_channel_id`, `slack_lookback_days` settings on the `Settings` class.

- **`backend/app/poller/poll.py`** — three additions:
  1. `_slack_lookback_days()` — reads `SLACK_LOOKBACK_DAYS` env var with a 14-day default.
  2. `sync_salt_keys_from_slack(cur, *, lookback_days=None, backfill=False)` — the main integration function. Walks the channel, joins to `easebuzz_onboarding` via `merchants.mid`, updates `salt_key_receipt` and `salt_key_from_kickstart`, returns a stats dict.
  3. `_merge_slack_stats()` — flattens slack-specific counts into the run-level stats dict with `slack_` prefix.
  4. Calls to `sync_salt_keys_from_slack()` inserted inside the `upserts` savepoint of both `run_sync` (lookback mode) and `run_backfill` (full-history mode).
  5. New `SLACK …` line in the CLI's DONE output, showing per-bucket counts.

### Unchanged but referenced

- **`backend/app/poller/schema.sql`** — no schema changes. `salt_key_receipt` (TEXT) already existed; we're just populating it.

---

## 6. Configuration reference

Add to `backend/.env`:

```
SLACK_BOT_TOKEN=xoxb-<the-bot-token-from-OAuth-and-Permissions>
SLACK_SALT_KEY_CHANNEL_ID=C0ALVC72T6K
SLACK_LOOKBACK_DAYS=14
```

| Var | Purpose | Default | Notes |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | Bot User OAuth Token | — | If blank, Slack sync is skipped (cron still runs). |
| `SLACK_SALT_KEY_CHANNEL_ID` | Channel ID | — | Currently `C0ALVC72T6K`. |
| `SLACK_LOOKBACK_DAYS` | How many days of history to walk in a normal cron run | 14 | Backfill ignores this and walks epoch → now. |

---

## 7. Verification done so far

### 7.1 Slack reachability

`auth.test` returned:

```
Auth OK as merchantonboardingrea (U0B630L48P2) in team GoKwik
```

`conversations.history` returned 29 messages between 2026-05-01 and 2026-05-22.

### 7.2 Parser correctness (offline)

Replayed every message in the live dump through the production regex + date parser:

```
batches parsed:   21
mid-date records: 279
unique MIDs:      246
unique dates:     16
unparseable headers: []

OK: no Key/Salt values present in parsed records
```

Assertion that `"Key:"` and `"Salt:"` substrings never appear in any yielded record passed.

### 7.3 Imports

```
python -c "from app.poller import poll, slack_io; print('imports OK')"
```

Compiles clean.

### 7.4 NOT yet done

- End-to-end run against the live DB (`python -m app.poller --backfill --force`).
- UI verification that filled rows render correctly in the frontend.
- Cron-side smoke test (one full hourly run).

---

## 8. How to run

```bash
cd /home/eeshu/Desktop/ops_infra/3/backend
source .venv/bin/activate

# One-time backfill — walks the entire Slack channel history.
python -m app.poller --backfill --force

# Regular sync — 14-day lookback. This is what cron calls.
python -m app.poller
```

Expected output adds a new line:

```
DONE  gokwik (new=X, upd=Y) | easebuzz from sheet ... | seeded=Z ...
SLACK records_seen=279 updated=42 no_onboarding_row=180 no_merchant_mid=57 dashboard_protected=0 errors=0
```

Stat meanings:

| Stat | Meaning |
|---|---|
| `slack_records_seen` | Total `(mid, date)` records yielded by the parser |
| `slack_updated` | Rows where `salt_key_receipt` was actually changed |
| `slack_no_onboarding_row` | MID exists in `merchants` but no `easebuzz_onboarding` row (pre-cutoff merchant) |
| `slack_no_merchant_mid` | MID not in `merchants` (merchant unknown to dashboard) |
| `slack_dashboard_protected` | Skipped because someone manually edited the dashboard |
| `slack_errors` | Set to `1` if the fetch itself blew up; `0` otherwise |
| `slack_skipped` | `1` if env vars missing |

On the first backfill run, expect `no_onboarding_row` + `no_merchant_mid` to be large — these are MIDs from before the 2026-05-05 cutoff that the dashboard intentionally doesn't track. They naturally drop as new merchants get seeded.

---

## 9. Action items / TODO

### Must do before relying on this

1. **Rotate the Slack token.** The original `xoxb-…` value was inadvertently printed to a Claude Code transcript during setup (a `sed` masking command failed because the user pasted the raw token without a `KEY=` prefix). Treat it as compromised:
   - https://api.slack.com/apps → `merchant-onboarding-reader` → **OAuth & Permissions** → **Regenerate** the Bot User OAuth Token.
   - Update `backend/.env` `SLACK_BOT_TOKEN=` with the new value.

2. **Remove the `chat:write` scope from the app.** It's not needed and a footgun. The dashboard is read-only.

3. **Run `python -m app.poller --backfill --force` once** to fill historical salt&key dates for every merchant on the dashboard whose MID appeared in any digest since the channel started.

4. **Sanity-check the dashboard UI** — confirm filled rows show the date in the SALT & KEY column and the "Needs review" badges clear where appropriate.

### Nice-to-have, not blocking

- Add a `slack_salt_key_runs` audit table mirroring `sync_runs` if ops want detailed per-run forensics. Current implementation folds stats into the main `sync_runs` row.
- Add a `salt_key_slack_permalink` column on `easebuzz_onboarding` so the frontend can show a "see in Slack" link. Decided against in v1 — adds schema churn and the dashboard already has a "Needs review" badge for the human workflow.
- Wire `slack_skipped` / `slack_errors` into a dashboard health endpoint so ops sees "Slack sync degraded" without grepping logs.
- Backoff/retry on Slack rate-limit errors (429). `slack-sdk` does basic retries by default; if we hit it in production, configure a `RateLimitErrorRetryHandler`.

### Open uncertainties

- The `slack_no_onboarding_row` bucket. Currently every batch yields ~13 MIDs and many won't have a dashboard row yet on the same day. The current behavior is "silently skip" — the next sync's Submerchant pass seeds them, and the *following* Slack pass picks up the date. This is fine as long as the lookback window (14 days) is longer than the worst-case delay from KYC completion → first dashboard appearance. Worth monitoring once in production.
- The bot's date format is currently `Month DD, YYYY` (long month name) or `Mon DD, YYYY`. If the bot changes format (e.g. ISO), `_parse_header_date()` will return `None` and the batch will be skipped with a warning — not a silent corruption, but worth alerting on.

---

## 10. Glossary

- **MID** — Merchant ID. Natural key from the *Gokwik Submerchant* tab. Used to join everything.
- **Salt & Key** — credentials Easebuzz issues per merchant when onboarding completes on their side. The dashboard cares about *when* they were issued, not the values.
- **Cutoff** — `2026-05-05` (env `KYC_SEED_CUTOFF`). Merchants whose Gokwik KYC complete date is on/before this are assumed already present from the one-time Easebuzz tab backfill and are not seeded.
- **Source flag** — `easebuzz_onboarding.source` is one of `sheet`, `dashboard`, or `seeded`. The `dashboard` value means an operator manually edited the row; never overwrite those.

---

## 11. Files to read for a deeper handoff

1. `backend/app/poller/slack_io.py` — parser module (~150 lines, self-documenting).
2. `backend/app/poller/poll.py` — search for `sync_salt_keys_from_slack` and the two `SAVEPOINT upserts` blocks where it's called.
3. `backend/scripts/dump_slack_messages.py` — to re-dump if message format changes.
4. `backend/app/poller/schema.sql` — for the `easebuzz_onboarding` and `merchants` table shapes.
5. `ops_infra/3/README.md` — overall project README; this Slack work doesn't change anything described there other than newly-populating the `salt_key_receipt` column.
