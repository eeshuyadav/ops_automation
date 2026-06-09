# Chargeback Automation — Architecture & Runbook

## 1. What this system does

A single Gmail mailbox — `chargeback-automation@gokwik.co` — receives every
chargeback notification GoKwik gets from its payment partners. For each
qualifying mail, this system:

1. Identifies the payment partner and chargeback severity (L1 / L2 / L3 /
   PayU / Worldline / Fraudulent).
2. Extracts the customer fields (phone, payment id, dispute amount, txn
   date, etc.) and the merchant identity.
3. Looks up the merchant's email and CSM (Customer Success Manager) from
   the Onboarding sheet + Planhat.
4. Sends a templated reply to the merchant — escalation-aware tone, with
   exact docs required, TAT warning, and the case identifiers.
5. Logs the action to a Google Sheet (monthly tab per flow).
6. After 48 hours, sends ONE reminder per thread if no one has replied.

It runs hourly on a single EC2 box under one cron entry. All six flows
share OAuth credentials and the same orchestration shape — only the
trigger pattern, body parser, and reply template differ.

---

## 2. The flows at a glance

| Flow | Trigger sender / pattern | Reply template | Tracker tab |
|------|---|---|---|
| **L1**  – Chargeback Raised               | `chargeback@easebuzz.in` (direct) OR `accountsreceivable@gokwik.co` (group) – subject `Chargeback Raised … Action Required` | `reply.html.j2`           | `{month} 26 L1` |
| **L2**  – Level 2 escalation              | same senders, subject `Chargeback Escalation … Level 2 … Urgent Action Required` | `reply_l2.html.j2`        | `{month} 26 L2` (Pre Arbitration section) |
| **L3**  – Level 3 / Arbitration           | same senders, subject `Chargeback Escalation … Level 3 … Urgent Action Required` | `reply_l3.html.j2`        | `{month} 26 L2` (Arbitration/L3 section) |
| **Fraudulent** (built, not wired to cron) | same senders, subject `Urgent Attention Required: Fraudulent Transaction` | `reply_fraudulent.html.j2`| `{month} 26 L2` (Fraudulent section) |
| **PayU**                                  | team-forwarded `Fwd: URGENT- FIRST LEVEL PAYU CHARGEBACK NOTIFICATION` – original `PayU-Chargeback@payu.in` in the quote | `reply_payu.html.j2`      | `{month} 26 PayU` |
| **Worldline**                             | `chargeback@mail.in.worldline-solutions.com` (or via Group), subject `Worldline New Chargeback Request For X = Y` | `reply_worldline.html.j2` | `{month} 26 Worldline` |

Tracker tabs roll over automatically at month boundaries — see §6.

---

## 3. Directory layout

```
/home/eeshu/Desktop/ops_infra/
├── 1/                          # Easebuzz family — production code home
│   ├── main.py                 # L1 / L2 / L3 / Fraudulent orchestrator
│   ├── config.yaml             # L1 config
│   ├── config_l2.yaml          # L2 config
│   ├── config_l3.yaml          # L3 config
│   ├── config_fraudulent.yaml  # Fraudulent (local only, not in cron)
│   ├── gmail_client.py         # paginated Gmail API wrapper + OAuth refresh
│   ├── sheets_client.py        # Sheets API + monthly-tab auto-create + section bands
│   ├── planhat_client.py       # multi-tier company lookup (name → email → prefix)
│   ├── body_parser.py          # HTML table / salutation extraction
│   ├── reminders.py            # +48h gentle reminder pass (shared by all flows)
│   ├── templates/              # 6 reply templates + reminder.html.j2
│   ├── credentials.json        # OAuth client (Desktop type)
│   ├── token.json              # refreshable user OAuth token
│   ├── planhat_token.txt       # Planhat bearer token
│   ├── run_all.sh              # cron entrypoint — iterates all enabled configs
│   └── logs/                   # rotated cron + chargeback logs
├── 4/                          # PayU automation — uses symlinks back to /1/
│   ├── main_payu.py
│   ├── config_payu.yaml
│   ├── payu_attachment.py      # .xls(x) attachment parser → 'New' sheet rows
│   ├── merchant_onboarding.py  # URL → email + entity-name lookup
│   ├── templates/reply_payu.html.j2
│   └── (symlinks: gmail_client, sheets_client, planhat_client, reminders, credentials.json, token.json)
├── 5/                          # Worldline automation — same symlink pattern
│   ├── main_worldline.py
│   ├── config_worldline.yaml
│   ├── worldline_body.py       # HTML <td>/<td> table → payment_id, bank_rrn, merchant_name
│   ├── templates/reply_worldline.html.j2
│   └── (symlinks back to /1/)
└── 2/.venv/                    # Python 3.12 virtualenv (deps: google-api-python-client, jinja2, openpyxl, bs4, pyyaml)
```

Folders /1/, /4/, /5/ are **logically separate flows** but share the same
underlying credentials, modules, and tracker spreadsheets. On EC2 they're
flattened into one directory (see §8).

---

## 4. The processing pipeline (single-thread view)

Each cron tick, for each config, `main.run_once()` runs:

```
1. Build Gmail query     →  {from:A from:B} subject:"..." -label:auto-replied ... after:YYYY/MM/DD
2. Search                →  list of trigger messages (paginated, up to 500/page × 10 pages)
3. Dedupe to threads
4. For each thread:
       (gate 1) subject regex match
       (gate 2) From in required_from
       (gate 3) Reply-To in required_reply_to  -- bypassed when From is in authoritative_senders
       (gate 4) body has no quoted reply
       (gate 5) thread.message_count == 1      -- no human reply yet
       (gate 6) extract fields from body (table + salutation)
       (gate 7) all required_fields present
       (gate 8) recipients resolve → To + Cc
   IF all 8 pass:
       send reply, apply auto-replied label, append tracker row
   ELSE if gate 8 fails:
       append manual-tracker row, apply auto-logged-manual label
5. Run the reminder pass (shared `reminders.run_reminders`)
```

### Field extraction (gate 6)

For Easebuzz/Fraudulent — the body has an HTML `<table>` with a header
row + value row. `body_parser.parse_body_fields()` zips them into a dict
using the `body_table_labels` map from config:

```yaml
body_table_labels:
  case_id:                 "Case ID"
  transaction_id:          "Transaction ID"
  merchant_transaction_id: "Merchant Transaction ID"
  customer_phone:          "Customer Phone"
  dispute_amount:          "Dispute Amount"
  ...
  merchant_name:           "__salutation__"   # special: parse "Dear X," from body
```

For Worldline — same 2-row table shape, but cells are plain `<td>` (not
`<th>`), and the columns are different (`Bank_RRN`, `SM_Transaction_ID_SRC_PRN`,
etc.). Handled by `worldline_body.parse_fields()`.

For PayU — the body has a URL but no per-case fields. Phone + Payment ID
come from a .xls(x) **attachment** with a `New` sheet (`payu_attachment.parse_attachment`).
Merchant identity comes from the **Onboarding sheet** (URL → email + entity).

### Recipient resolution (gate 8)

The bot computes the reply's **To** and **Cc** with multi-tier lookups,
designed to keep the right humans in the loop without spamming.

**Easebuzz (L1/L2/L3) — `compute_addresses`:**

```
To  := merchant POC email
        ← try sheet `Master Sheet LT` row whose Merchant Name matches the salutation
        ← else partial-prefix match
        ← else email-domain match against the original mail's To: domain
        ← else static merchant_contacts fallback in config (rarely set)
Cc  := original mail's Cc (preserved)
     + internal_always_cc (musadiq, sachin.mk, accountsreceivable, chargeback@easebuzz.in)
     + merchant CSM from sheet's `New CSM` column
        ← else Planhat owner + coOwner emails for the merchant name
        (status_skip_csm_patterns drops the CSM if the row is marked "Transferred")
```

If the merchant Status in sheet contains "Transferred" → drop the sheet's CSM
and fall through to Planhat. If no merchant POC can be derived at all →
log to manual tracker, label `auto-logged-manual`, **don't send**.

**PayU — `main_payu.py`:**

```
To  := merchant POC email
        ← sheet lookup by URL (strict — no subdomain fallback per ops policy)
        ← else: emails extracted from the forwarded body's `To:` line (strip @gokwik.co, @payu.in)
Cc  := internal_always_cc + PayU-Chargeback@payu.in + CSM from Planhat
        (CSM looked up by sheet's Entity Name; falls through silently if not found)
```

**Worldline — `main_worldline.py`:**

```
To  := merchant POC email(s) from ORIGINAL mail's To header
        — drop @gokwik.co, @easebuzz.in, @worldline-solutions.com, @tpsl.in subdomains
Cc  := ORIGINAL mail's Cc, preserved verbatim (TPSL escalation chain stays)
        + internal_always_cc
        + CSM from Planhat (by merchant name parsed from subject "For X = Y")
```

### Subject of the outgoing reply

`Re: <original subject>` — preserves the thread for Gmail. We do NOT
generate a fresh subject (would break threading and merchant context).

### Body of the outgoing reply

Rendered from a Jinja2 template per flow. All current templates use the
new **Sachin-signature, escalation-style format** (TAT warnings, docs
required, important reminders block, details). Variable substitution:

| Template var      | Source                                                |
|-------------------|-------------------------------------------------------|
| `merchant_name`   | salutation parse / subject regex / sheet brand col / URL apex stem |
| `merchant_transaction_id` | body table — "Merchant Transaction ID" (KWIK... id) |
| `customer_phone`  | body table — "Customer Phone" (or attachment row for PayU) |
| `transaction_date`| body table — "Transaction Date Time" (time stripped by `_strip_time`) |
| `dispute_amount`  | body table — "Dispute Amount"                          |
| `payment_id`      | Worldline body `SM_Transaction_ID_SRC_PRN`             |

---

## 5. The reminder system

Lives in `reminders.py`, called at the end of every flow's `run_once()`.

**Trigger condition for a reminder fire:**

1. Thread has the `auto-replied{,-l2,-l3,-payu,-worldline}` label (we sent the original).
2. Thread does NOT have the `auto-reminded{,-l2,-l3,-payu,-worldline}` label.
3. Thread has exactly **1 external message** — that is, only the original
   chargeback notification is from someone other than us. Any reply from a
   real person flips this off.
4. Our send was ≥ `reminder_delay_hours` (default 48) ago.
5. Our send is on or after `reminder.after_date` (a hard date floor per flow).

When all 5 hold → reply with `templates/reminder.html.j2` to the same
recipients we used originally + apply the `auto-reminded-*` label so we
never re-fire on this thread.

**Why count "external messages" instead of "thread.message_count != 2"?**

The bot's own follow-ups (e.g., the one-time `cc_easebuzz_backfill` mails)
add 3rd, 4th… messages to threads. The naive `count != 2` check would
permanently silence the reminder on every backfilled thread. Counting
external (non-self) messages makes the system robust against the bot
appending to its own threads.

The `auto-reminded-*` label still guarantees one-shot — even if external
count stays at 1, the label exclusion in the search query stops the
reminder from firing twice.

---

## 6. Tracker spreadsheets

Two Google Sheets, both shared with `chargeback-automation@gokwik.co` (Editor).

### Success tracker — `1r5bELHKHJhWrydAs7TWOyyC-fkFrexbIn6cUeTp0MLw`

Monthly tabs by flow:

```
April 26 L1        ← L1 sends
May 26 L1          ← L1 sends
April 26 L2        ← L2 / L3 / Fraudulent sends (sub-sectioned)
May 26 L2
   ├─ "Pre Arbitration L2"            (L2 section — top)
   ├─ "Fraudulent Transaction!!"      (Fraudulent section)
   ├─ "Arbitration/ L3 CB"            (L3 section)
   └─ "Critical: RBI/BO Complaint"    (placeholder, no orchestrator yet)
May 26 PayU
May 26 Worldline
```

**Monthly rollover** is automatic. When `_ensure_log_tab()` doesn't find a
tab for the current month, it **duplicates the most recent same-pattern
tab** (preserving column widths, header colour, and the L2 section bands)
and compacts the skeleton: keeps all structural rows + 1 empty buffer row
between sections, deletes the historical data rows. The new month's tab
ships pre-formatted, identical to the prior month, ready for fresh appends.

**Sub-section targeting** (L3 / Fraudulent / future RBI):

```yaml
log_sheet:
  tab_pattern:    "{month} 26 L2"
  target_section: "Arbitration/ L3 CB"   # find this band, append below its cyan header
```

The append goes into the section band's column-header row's range (e.g.
`'May 26 L2'!A23`). Gmail's `INSERT_ROWS` inserts a new row there,
pushing the buffer + the next section's band down by 1.

### Manual review tracker — `1BOh8xO4NEWtVQ8UfI0ef2cXGRx9guoi8FEZ6d19djbM`

One tab per flow:

```
Manual Review              ← L1
Manual Review L2
Manual Review L3
Manual Review Fraudulent   ← only if Fraudulent gets cron-wired
Manual Review PayU
Manual Review Worldline
```

Rows here mean **the bot refused to send** (gate 8 failed) and a human
needs to take it over. The orchestrator stamps `auto-logged-manual{,-l2,…}`
on these threads so cron doesn't re-log them every hour.

---

## 7. Configuration model (YAML)

Every flow has the same shape. Annotated reference:

```yaml
# --- Auth (shared OAuth across all flows) ----------------------------------
auth:
  credentials_path: "credentials.json"   # OAuth client (Desktop type)
  token_path:       "token.json"          # refresh-token store

# --- Identity ---------------------------------------------------------------
self_email:        "chargeback-automation@gokwik.co"
self_display_name: "Chargeback Automation"
contact_email:     ""                     # vestigial — templates ignore it

# --- Trigger ---------------------------------------------------------------
watch_senders:                            # OR-ed in Gmail search
  - "accountsreceivable@gokwik.co"
  - "chargeback@easebuzz.in"
extra_query:  'subject:"X" subject:"Y" -label:auto-replied -label:auto-logged-manual after:2026/05/23'
subject_regex: 'X\b.*?\bY\b'              # code-side regex (Gmail search is loose)
required_from:                            # at least one of these must be in the From header
  - "accountsreceivable@gokwik.co"
  - "chargeback@easebuzz.in"
required_reply_to: "chargeback@easebuzz.in"
authoritative_senders:                    # if From is here, skip required_reply_to check
  - "chargeback@easebuzz.in"

# --- Labels ----------------------------------------------------------------
auto_replied_label:        "auto-replied"        # on send
auto_logged_manual_label:  "auto-logged-manual"  # on gate-8 failure
auto_reminded_label:       "auto-reminded"       # on +48h reminder
reminder_delay_hours:      48
reminder:
  template_path: "templates/reminder.html.j2"
  after_date:    "2026/05/01"             # hard cutoff for reminder backfill

# --- Body parser map -------------------------------------------------------
body_table_labels:
  merchant_transaction_id: "Merchant Transaction ID"
  customer_phone:          "Customer Phone"
  merchant_name:           "__salutation__"   # special: parse "Dear X," from body
  ...

# --- Merchant lookup (Easebuzz) --------------------------------------------
sheet:
  spreadsheet_id: "1OczJ8a8…"
  range:          "'Master Sheet LT'!A1:Z"
  name_col:       "Merchant Name"
  to_col:         "Email ID 1"
  cc_col:         "New CSM"
  status_col:     "Status"
  status_skip_csm_patterns:
    - "transferred"

# --- Planhat (CSM fallback) ------------------------------------------------
planhat:
  token_path: "planhat_token.txt"
  base_url:   "https://api.planhat.com"

# --- Always-Cc internal ops ------------------------------------------------
internal_always_cc:
  - "musadiq.ahmed@gokwik.co"
  - "accountsreceivable@gokwik.co"
  - "sachin.mk@gokwik.co"
  - "chargeback@easebuzz.in"

# --- Reply -----------------------------------------------------------------
reply:
  template_path:   "templates/reply.html.j2"
  subject_override: ""                    # blank → use original subject
  reply_in_thread: true
  required_fields:                        # gate 7
    - merchant_transaction_id
    - customer_phone
    - merchant_name

# --- Tracker (success) -----------------------------------------------------
log_sheet:
  spreadsheet_id: "1r5bELHK…"
  tab_pattern:    "{month} 26 L1"
  # Optional: target_section: "Fraudulent Transaction!!"   for sub-section append
  columns:
    - "__today__"                # D-MMM-YYYY of send time
    - "transaction_id"           # Easebuzz Id (E260…)
    - "merchant_transaction_id"  # Payment id (KWIK…)
    - "transaction_date"         # time-stripped
    - "dispute_amount"
    - "dispute_amount"           # CBK amount (same)
    - "merchant_email_id"        # first To address
    - "merchant_name"
    - "__static__:Pending with ME"
    - "__static__:WIP"
    - "__static__:"              # blank cell
    - "__static__:Sachin(bot)"

# --- Tracker (manual review) -----------------------------------------------
failed_log_sheet:
  spreadsheet_id: "1BOh8xO4…"
  tab_pattern:    "Manual Review"
  columns: [...same shape, with Sachin(bot-skipped) and "no recipients resolved"...]
```

PayU and Worldline add a few extra blocks (`merchant_sheet`, `internal_domains`,
`allowed_senders`) — see their config files for inline docs.

---

## 8. Deployment

### Single EC2 host

```
chargeback host                 (Amazon Linux 2023, ARM/Graviton, IST timezone)
└── /home/ec2-user/chargeback-automation/
    ├── main.py + config.yaml + config_l2.yaml + config_l3.yaml
    ├── main_payu.py + config_payu.yaml + payu_attachment.py + merchant_onboarding.py
    ├── main_worldline.py + config_worldline.yaml + worldline_body.py
    ├── gmail_client.py, sheets_client.py, planhat_client.py, reminders.py, body_parser.py, logging_setup.py
    ├── credentials.json, token.json, planhat_token.txt
    ├── templates/  (all reply templates, including reminder.html.j2)
    ├── run_all.sh
    ├── logs/
    └── .venv/  (Python 3.11)
```

On EC2 the layout is **flat** — no /1/, /4/, /5/ subdirs. The symlinks
that exist locally are resolved at scp time.

### Cron

Single hourly entry:

```
0 * * * * /home/ec2-user/chargeback-automation/run_all.sh >> /home/ec2-user/chargeback-automation/logs/cron.out 2>&1
```

`run_all.sh` iterates the enabled configs in series:

```bash
EASEBUZZ_CONFIGS=(config.yaml config_l2.yaml config_l3.yaml)
for cfg in "${EASEBUZZ_CONFIGS[@]}"; do
  .venv/bin/python main.py --once --config "$cfg"
done

# PayU — DISABLED in cron pending more validation. Uncomment to re-enable:
# .venv/bin/python main_payu.py --once --config config_payu.yaml

.venv/bin/python main_worldline.py --once --config config_worldline.yaml
```

Each flow runs sequentially so they don't race on token refresh or Gmail
quota.

### Adding a new flow to cron

1. Drop the new `config_X.yaml` and `main_X.py` (or reuse main.py) into
   `/home/ec2-user/chargeback-automation/`.
2. Add the corresponding `templates/reply_X.html.j2`.
3. Append a line to `run_all.sh`:
   `.venv/bin/python main_X.py --once --config config_X.yaml`
4. The reminder pass is automatic — no extra wiring needed.

---

## 9. OAuth and token management

The bot runs as `chargeback-automation@gokwik.co`. `credentials.json`
is an OAuth Desktop client. `token.json` holds a refresh token that
covers both Gmail and Sheets scopes:

```
SCOPES = [
  "https://www.googleapis.com/auth/gmail.readonly",
  "https://www.googleapis.com/auth/gmail.modify",
  "https://www.googleapis.com/auth/gmail.send",
  "https://www.googleapis.com/auth/gmail.labels",
  "https://www.googleapis.com/auth/spreadsheets",
]
```

### Refreshing an expired token

If you see `RefreshError: invalid_grant: Token has been expired or revoked`:

1. On any laptop with a browser, delete the local `token.json`.
2. Run `python main.py --once --dry-run --config config.yaml` — a browser
   opens. Sign in **as `chargeback-automation@gokwik.co`** (NOT a personal
   account). A fresh `token.json` gets written.
3. `scp token.json chargeback:/home/ec2-user/chargeback-automation/`.
4. Cron resumes automatically on the next tick.

---

## 10. Observability

### Logs

- `logs/cron.out` — captures every cron run's stdout + stderr.
  Grep here for `===== easebuzz config_X =====`, `[poll]`, `[sent]`, `[skip]`.
- `logs/chargeback.log` — INFO/ERROR from `logging_setup`. Used for
  exception tracebacks.
- `logs/cron.out` is **append-only**, can grow large. Rotate manually if needed.

### Gmail labels (read-only audit on the bot mailbox)

```
auto-replied / -l2 / -l3 / -payu / -worldline        ← bot replied
auto-logged-manual / -l2 / -l3 / -payu / -worldline  ← bot punted to manual tracker
auto-reminded / -l2 / -l3 / -payu / -worldline       ← +48h reminder fired
auto-cc-easebuzz                                     ← one-time historical backfill (May 2026)
```

### Tracker sheets (business audit)

The two spreadsheets in §6. New rows always include the send date and a
clear `Ops POC = Sachin(bot)` so manual rows (Ops POC = a person's name)
can be filtered out.

---

## 11. Failure modes and how to debug

### "No revert for case X"

1. Search Gmail: `subject:X` from the bot mailbox.
2. Check thread message count.
3. If only 2 messages and msg #1 is from `chargeback-automation@gokwik.co`
   → bot replied; merchant hasn't responded yet. Reminder fires +48h.
4. If only 1 message → bot didn't act. Walk the gates:
   - Subject regex matches? (test against `subject_regex` in config)
   - From header in `required_from`?
   - Reply-To header in `required_reply_to` (or From in `authoritative_senders`)?
   - Body has a quoted-reply chain? (we reject forwards)
   - Required fields extracted from body table?

Use `/home/eeshu/Desktop/ops_infra/1/_trace_one.py <TX>` to walk all gates
against any specific thread.

### Gmail API HTTP 429 (rate limit)

User-rate quota is 250 quota units/sec per OAuth user, per Google's docs.
Heavy interactive testing during deploys can exhaust it. The error
includes a `Retry-Afterimme: <ISO timestamp>`. Wait until then. The cron's
hourly cadence stays well under the quota — only manual scripts cause this.

### "Bot replied to wrong merchant"

The merchant-resolution multi-tier lookup can match the wrong row if:

- The salutation in body doesn't match the sheet's `Merchant Name` exactly,
  AND
- The body's `Dear X,` is too generic ("Dear Merchant", "Dear Team"), AND
- The mail's `To:` domain belongs to a different merchant than the one
  named in the salutation.

In this case the bot falls through to email-domain matching, which picks
a row that shares the domain. If the merchant uses a shared domain (e.g.
`@easebuzz.in` for test accounts), this can cross-wire. Mitigation:

- Make the body's salutation unambiguous (`Dear MyMerchant Pvt Ltd,`).
- Add a static `merchant_contacts` block in the YAML for known
  ambiguous merchants.

### Onboarding sheet missing a Website value

PayU has a strict URL match. If `myfrido.com` isn't a row in the sheet,
the bot falls through to the **forwarded-To fallback** (extract @-addresses
from the original PayU mail's `To:` line, drop @gokwik.co + @payu.in,
use the remainder as the merchant TO). Brand name for the reply is
derived from the URL apex (e.g., `myfrido.com` → `Myfrido`).

If even the fallback returns no addresses → manual tracker.

---

## 12. Local-only utility scripts (never deployed)

| Script | Purpose |
|---|---|
| `_trace_one.py <TX>` | Walk all gates for a specific transaction id |
| `_backfill_send.py --days N` | Re-run the cron for the past N days (used after a routing-change incident) |
| `_backfill_recheck.py --days N` | Smarter check — distinguish "team replied" from "thread bumped by Easebuzz follow-up" |
| `_preview_one.py --q "..."` | Render exactly what the outgoing reply would look like for a given thread |
| `cc_easebuzz_backfill.py` | One-time historical: BCC `chargeback@easebuzz.in` on every prior bot reply |
| `send_reminder_one.py --tx X` | Force-send a reminder on a specific thread |
| `send_one.py` | Manual send for a single transaction id, bypassing some gates |

These exist **only on local development machines** (`/home/eeshu/Desktop/ops_infra/1/`).
They are NOT in `run_all.sh` and NOT on EC2.

---

## 13. Important historical decisions / gotchas

- **Why a Google Group route?** Easebuzz used to send only to the
  `accountsreceivable@gokwik.co` Google Group. In May 2026 they began
  sending directly from `chargeback@easebuzz.in` without a Reply-To
  header. The bot was failing identity gate 3 on those. Fix: added
  `authoritative_senders` to bypass the Reply-To requirement when the
  From itself is trusted.

- **Why two separate Google Sheets?** The success tracker has a strict
  monthly-tab layout that needs duplicating each month. The manual-review
  tracker has flat tabs that don't roll over. Keeping them separate makes
  the duplication logic robust (only duplicates the success sheet's tabs).

- **Why preserve the original Cc on Worldline replies?** Worldline's
  notifications carry a TPSL escalation chain on the Cc
  (`sachin.ghadigaonkar@tpsl.in`, `kalpesh.patne@tpsl.in`, etc.). Those
  contacts handle escalations on Worldline's side. If we drop them, the
  escalation thread breaks. So the bot preserves the original Cc verbatim
  and only **adds** our internal team + Planhat CSM on top.

- **Why count "external messages" for reminders?** See §5 — protects
  against bot-induced thread bumps from breaking the reminder logic.

- **Why is PayU disabled in cron right now (as of May 2026)?** The
  Onboarding sheet has empty Website columns for several active merchants
  — strict URL match fails for them, and the forwarded-To fallback isn't
  guaranteed safe for all merchants. Holding until the sheet is filled in
  more completely. Files are deployed and ready; one un-comment in
  `run_all.sh` re-enables it.

- **OAuth refresh tokens for Google Workspace apps in "Testing" mode
  expire after 7 days of inactivity.** The bot mailbox is active so this
  typically doesn't fire, but a manual revoke (or 7-day idle period) can
  kill the token. Recovery procedure in §9.

- **Date filter `after:YYYY/MM/DD`** in Gmail's query language is
  **inclusive** at the date level (UTC). Bumping cutoff to "today" still
  includes today's mails. Bump to "tomorrow" to truly pause until tomorrow.

---

## 14. Quick reference — what runs when

```
00:00 IST  ─ cron tick. run_all.sh fires sequentially:
              1. L1   (~30 sec — search + 0-N sends + reminder pass)
              2. L2   (~30 sec)
              3. L3   (~30 sec — only Arbitration cases)
              4. (PayU disabled)
              5. Worldline   (~20 sec)
01:00 IST  ─ next tick, same again.
...
```

Each tick is independent. There's no in-memory state between runs — every
gate is re-evaluated from scratch against Gmail + the sheets. Idempotency
is enforced by Gmail labels (`-label:auto-replied` etc. in the query).

---

## 15. Glossary

| Term | Meaning |
|---|---|
| **TAT** | Turn-Around Time — bank's deadline to defend a chargeback |
| **POD** | Proof of Delivery |
| **Chargeback** | Customer's bank reversed a transaction; merchant must defend or absorb the loss |
| **L1 / L2 / L3** | Easebuzz's three escalation levels: Raised → Escalation Level 2 → Arbitration |
| **Arbitration / L3 CB** | Final escalation; binding decision by the card network |
| **CSM** | Customer Success Manager (GoKwik side, per merchant) |
| **POC** | Point Of Contact |
| **Group** | Google Workspace Group, e.g. `accountsreceivable@gokwik.co` — Gmail rewrites From on forwarded messages to look like they came from the group |
| **bot self** | `chargeback-automation@gokwik.co` — the mailbox the system runs as |
