"""Merchant-onboarding poller.

Two modes:

    python -m app.poller             # default cron job (weekly)
        Pulls only the Gokwik Submerchant list. For each MID with a
        non-blank Gokwik KYC complete date and no matching onboarding
        row yet, auto-creates an `easebuzz_onboarding` row seeded from
        the Submerchant data + the (stubbed) external API.

    python -m app.poller --backfill  # one-time historical import
        Additionally pulls the full Easebuzz tab of the Ops Updates
        sheet and upserts every row into `easebuzz_onboarding`. Run
        this ONCE after the DB is created. After that, the Easebuzz
        Google Sheet is treated as archived — the dashboard is the
        source of truth and the regular cron never reads it again.

Library:
    from app.poller.poll import run_sync, run_backfill
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from dateutil import parser as date_parser

from app.poller import api_client, sheets_io, slack_io
from app.poller.normalize import normalize_name


import re

# Recognised date shapes for the non-ISO fast path. Anything that doesn't
# match one of these never reaches dateutil — that's the safety net against
# fuzzy parsing turning a literal like "Not Embedded" into a date by latching
# onto one stray digit.
#
# Patterns (left to right):
#   1) "27-January-26", "10-Oct-2022", "9 Dec 2022"  -- dd[-/ ]Month[-/ ]yy(yy)
#   2) "Apr 15, 2024",   "April 15 2024"             -- Month dd[,] yyyy
#   3) "21-01-2023",     "04/24/2024"                -- dd[-/]mm[-/]yyyy
_DATE_PATTERNS = (
    re.compile(r"^\d{1,2}[-/\s][A-Za-z]+[-/\s]\d{2,4}$"),
    re.compile(r"^[A-Za-z]+\s+\d{1,2},?\s+\d{4}$"),
    re.compile(r"^\d{1,2}[-/]\d{1,2}[-/]\d{4}$"),
)
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


def _parse_sheet_date(raw: str) -> "date | None":
    """Parse one of the many date formats we see across sheet + API.

    Inputs:
      - Sheet: "27-January-26", "10-Oct-2022", "21-01-2023", "Apr 15, 2024",
               "04-24-2024", "2024-09-25 05:30", "9-Dec-2022"
      - Kickoff API: "2026-05-12" (ISO) or "2026-05-12T13:45" (ISO datetime)

    Strategy:
      1. ISO YYYY-MM-DD is matched literally — dateutil's `dayfirst=True`
         flips ISO dates around (e.g. "2026-05-12" -> 2026-12-05), so we
         never hand them to dateutil.
      2. For everything else, the input must match one of `_DATE_PATTERNS`
         (after stripping any time component). If it doesn't match, we
         return None — `fuzzy=True` was removed because it turned junk like
         "Not Embedded" or "#N/A" into actual dates by grabbing stray digits.

    Returns a date or None. Never raises.
    """
    from datetime import date

    s = (raw or "").strip()
    if not s:
        return None

    # Strip any time portion from ISO datetimes ("2026-05-12T13:45" or
    # "2024-09-25 05:30"). We deliberately do NOT split on every space —
    # "Apr 15, 2024" must reach the pattern gate intact.
    if "T" in s:
        s_no_time = s.split("T", 1)[0].strip()
    elif re.match(r"^\d{4}-\d{1,2}-\d{1,2}\s", s):
        s_no_time = s.split(" ", 1)[0].strip()
    else:
        s_no_time = s

    # Fast path: ISO YYYY-MM-DD — unambiguous, never run dateutil on this.
    m = _ISO_DATE_RE.match(s_no_time)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    # Tight gate: must look date-shaped before we let dateutil near it.
    if not any(p.match(s_no_time) for p in _DATE_PATTERNS):
        return None

    try:
        # dayfirst=True matches the dominant dd-MM-yyyy / dd-Mon-yyyy patterns
        # in the sheet. fuzzy is intentionally OFF — see docstring.
        dt = date_parser.parse(s_no_time, dayfirst=True, yearfirst=False)
        return dt.date() if hasattr(dt, "date") else dt
    except (ValueError, OverflowError, TypeError):
        return None
    except Exception:  # pragma: no cover - dateutil's exception zoo
        return None


def _load_holidays() -> set[date]:
    """Read public-holiday dates from `backend/holidays.txt` so they can be
    excluded from `time_taken_by_eb` (the EB-days business-day count for
    seeded rows). One ISO date per line; `#` comments and blank lines ignored.

    Missing file is fine (treated as no holidays). The path resolves
    relative to backend/, which matches the layout where the poller
    actually runs from.
    """
    path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "holidays.txt")
    )
    out: set[date] = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                try:
                    out.add(date.fromisoformat(line))
                except ValueError:
                    # Bad line — log and skip rather than abort the run.
                    print(f"WARN: holidays.txt: ignoring unparseable line {line!r}")
    except FileNotFoundError:
        pass
    return out


def compute_business_days(
    start_text: Any, end_text: Any, holidays: set[date] | None = None,
) -> str | None:
    """Business days between two date strings, Sat/Sun and `holidays` excluded.

    Counts the number of weekday non-holiday calendar days STRICTLY between
    the two dates plus the end day — matching the user spec "diff is 4 days
    and in those sat sun came then only 2 days count": for Thu→Mon (raw
    diff 4), Fri + Mon are counted, Sat + Sun excluded → 2.

    Returns:
      str(n)  — when both inputs parse
      "0"     — when both inputs parse and resolve to the same date
      None    — when either date can't be parsed (so we don't clobber
                existing values with garbage)
    """
    holidays = holidays or set()
    s = _parse_sheet_date(str(start_text) if start_text else "")
    e = _parse_sheet_date(str(end_text) if end_text else "")
    if s is None or e is None:
        return None
    if s == e:
        return "0"

    lo, hi = (s, e) if s <= e else (e, s)
    days = 0
    d = lo + timedelta(days=1)
    while d <= hi:
        if d.weekday() < 5 and d not in holidays:
            days += 1
        d += timedelta(days=1)
    return str(days)


# Suffixes commonly tacked onto merchant names that don't matter for
# identity matching. `normalize_name` keeps them (it just lowercases +
# strips non-alphanumerics), so a Submerchant entry "Finemoe.com" comes
# out as "finemoecom" while Mintdash's "Finemoe" normalizes to
# "finemoe" — same merchant, two different keys. `_loose_kickoff_lookup`
# below builds a fallback index that strips these on both sides.
_NAME_SUFFIXES_TO_STRIP = (
    "com", "in", "co", "store", "shop", "india", "online",
    "privatelimited", "pvtltd", "pvtlimited", "pvtlimitedcompany",
    "ltd", "limited", "llc", "llp", "company",
)


def _strip_trailing_suffix(s: str) -> str:
    """If `s` ends in one of the common merchant-name suffix tokens,
    return `s` with that suffix removed. Single pass — picks the
    longest matching suffix so we don't half-strip 'privatelimitedltd'.

    Examples:
        'finemoecom' -> 'finemoe'
        'broyaarshop' -> 'broyaar'
        'amourdefloracom' -> 'amourdeflora'
        'finemoe' -> 'finemoe' (no suffix to strip)
    """
    longest = ""
    for sfx in _NAME_SUFFIXES_TO_STRIP:
        if s.endswith(sfx) and len(s) > len(sfx) + 1 and len(sfx) > len(longest):
            longest = sfx
    return s[:-len(longest)] if longest else s


def _loose_kickoff_lookup(kickoff_lookup: dict) -> dict:
    """Build an expanded Mintdash lookup that's tolerant to name
    suffix differences. The exact-match canonical keys are preserved;
    additional entries are added for the suffix-stripped form of each
    Mintdash name. Exact matches always win — the loose entries only
    fill in where a canonical key is absent.

    This is the read-only fallback the seed step + hourly refetch use
    AFTER the canonical-key lookup misses. It avoids re-normalizing
    every existing DB row, which would require a one-time migration.
    """
    loose: dict = dict(kickoff_lookup)  # canonical keys preserved
    for k, v in kickoff_lookup.items():
        s = _strip_trailing_suffix(k)
        if s != k and s not in loose:
            loose[s] = v
    return loose


def _lookup_kickoff(canonical_lookup: dict, loose_lookup: dict, my_norm: str) -> dict:
    """Try the canonical lookup first (exact match); fall back to the
    loose index, optionally stripping our own key's suffix too. Returns
    {} when no match in either direction.
    """
    info = canonical_lookup.get(my_norm)
    if info:
        return info
    info = loose_lookup.get(my_norm)
    if info:
        return info
    stripped = _strip_trailing_suffix(my_norm)
    if stripped != my_norm:
        return loose_lookup.get(stripped) or {}
    return {}


def _canonical_sheet_date(raw: Any) -> Any:
    """Reformat any parseable date string into the sheet's canonical
    `dd-MMM-yy` form (e.g. `21-May-26`). Used for seeded-row date columns so
    the dashboard reads consistently across Kickstart / Docs Recd /
    Email-to-EB / Salt&Key.

    Returns the input unchanged if it can't be parsed — never destroys a
    value the operator typed in.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return raw
    d = _parse_sheet_date(s)
    if d is None:
        return raw
    return d.strftime("%d-%b-%y")


def _seeded_status(kickstart_date: Any, salt_key_receipt: Any) -> str:
    """Status rule for auto-seeded 'Needs review' rows.

    Yes once BOTH endpoints have arrived (kickstart from Kickoff API +
    salt&key from Slack). No until then. Sheet-imported and dashboard-edited
    rows are not subject to this rule — their status is whatever the sheet
    or the operator set.
    """
    has_kickstart = bool(kickstart_date and str(kickstart_date).strip())
    has_salt_key  = bool(salt_key_receipt and str(salt_key_receipt).strip())
    return "Yes" if (has_kickstart and has_salt_key) else "No"


def _slack_lookback_days() -> int:
    """Days of channel history to scan during a regular sync.

    Default 14 — comfortably covers cron downtime, weekends, and the
    Easebuzz bot's occasional re-posts. Override via SLACK_LOOKBACK_DAYS.
    Set to 0 or negative to use the default.
    """
    raw = os.environ.get("SLACK_LOOKBACK_DAYS", "").strip()
    if raw:
        try:
            n = int(raw)
            if n > 0:
                return n
        except ValueError:
            pass
    return 14


def _kyc_seed_cutoff() -> "date":
    """Resolve cutoff date from env, falling back to 2026-05-05."""
    from datetime import date
    raw = os.environ.get("KYC_SEED_CUTOFF", "").strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return date(2026, 5, 5)


def compute_days_kickstart_to_salt_key(
    kickstart_raw: str | None, salt_key_raw: str | None,
) -> str | None:
    """Return the absolute day-difference between Kickstart and Salt&Key Receipt
    as a string (matches the sheet's text-typed column).

    Always non-negative — the calendar gap is what matters, not the direction.
    Returns None if either date doesn't parse, so we don't clobber a stored
    sheet value with garbage.
    """
    ks = _parse_sheet_date(kickstart_raw or "")
    sk = _parse_sheet_date(salt_key_raw or "")
    if ks is None or sk is None:
        return None
    return str(abs((sk - ks).days))


MERCHANT_FIELDS = (
    "mid", "merchant_size", "eb_go_live_date", "kyc_spoc",
    "gokwik_kyc_complete_date", "merchant_name", "entity_name", "email",
    "website", "onboarding", "entity", "name_normalized",
)

EASEBUZZ_FIELDS = (
    "merchant_name", "name_normalized", "merchant_size", "onboarding_status",
    "kickstart_date", "kickstart_time", "docs_received_date", "docs_received_time",
    "days_taken_ks_to_ds", "time_taken_ks_to_ds", "kyc_completed_by_ops",
    "days_taken_kyc", "date_email_sent_to_eb", "salt_key_receipt",
    "time_taken_by_eb", "salt_key_from_docs_recd", "salt_key_from_kickstart",
    "reasons_for_delay_in_eb", "promise", "delivery", "remarks",
    "delay_at_gk", "delay_by_merchant", "ops_remarks",
)


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------
def _merchant_upsert(cur, rec: dict[str, Any]) -> str:
    rec["name_normalized"] = normalize_name(rec.get("merchant_name", ""))
    values: list[Any] = [rec.get(f) for f in MERCHANT_FIELDS]
    placeholders = ", ".join(["%s"] * len(MERCHANT_FIELDS))
    update_set = ", ".join(
        f"{f} = EXCLUDED.{f}" for f in MERCHANT_FIELDS if f != "mid"
    )
    cur.execute(
        f"""
        INSERT INTO merchants ({", ".join(MERCHANT_FIELDS)}, last_synced_at, updated_at)
        VALUES ({placeholders}, now(), now())
        ON CONFLICT (mid) DO UPDATE SET
            {update_set},
            last_synced_at = now(),
            updated_at     = now()
        RETURNING (xmax = 0) AS inserted
        """,
        values,
    )
    return "inserted" if cur.fetchone()["inserted"] else "updated"


def _easebuzz_upsert_from_sheet(cur, rec: dict[str, Any]) -> tuple[str, bool]:
    """Upsert one row pulled from the Easebuzz tab during BACKFILL only.

    Resolves merchant_id by normalized-name match against `merchants`.
    """
    rec["name_normalized"] = normalize_name(rec.get("merchant_name", ""))
    if not rec["name_normalized"]:
        return "skipped", False

    cur.execute(
        "SELECT id FROM merchants WHERE name_normalized = %s LIMIT 1",
        (rec["name_normalized"],),
    )
    row = cur.fetchone()
    merchant_id = row["id"] if row else None

    # Parse kickstart_date for sortable column. NULL when unparseable.
    kickstart_parsed = _parse_sheet_date(rec.get("kickstart_date") or "")

    # Sheet's `salt_key_from_kickstart` can be negative when the source formula
    # broke (e.g., bad date order). Override with the absolute day-gap so the
    # DB always carries a non-negative value when computable.
    computed = compute_days_kickstart_to_salt_key(
        rec.get("kickstart_date"), rec.get("salt_key_receipt"),
    )
    if computed is not None:
        rec["salt_key_from_kickstart"] = computed

    cols = list(EASEBUZZ_FIELDS) + ["merchant_id", "source", "kickstart_date_parsed"]
    values = [rec.get(f) for f in EASEBUZZ_FIELDS] + [merchant_id, "sheet", kickstart_parsed]
    placeholders = ", ".join(["%s"] * len(cols))

    # On conflict during backfill, refresh from the sheet — backfill is
    # one-shot anyway, so we don't need the dashboard-protection logic here.
    update_set = ", ".join(
        f"{f} = EXCLUDED.{f}" for f in cols
        if f not in ("name_normalized", "source")
    )
    cur.execute(
        f"""
        INSERT INTO easebuzz_onboarding ({", ".join(cols)}, last_synced_at, updated_at)
        VALUES ({placeholders}, now(), now())
        ON CONFLICT (name_normalized) DO UPDATE SET
            {update_set},
            last_synced_at = now(),
            updated_at     = now()
        RETURNING (xmax = 0) AS inserted
        """,
        values,
    )
    return (
        ("inserted" if cur.fetchone()["inserted"] else "updated"),
        merchant_id is not None,
    )


def sync_salt_keys_from_slack(
    cur,
    *,
    lookback_days: int | None = None,
    backfill: bool = False,
) -> dict[str, int]:
    """Pull salt&key receipt dates from the Easebuzz Slack channel.

    Reads `SLACK_BOT_TOKEN` + `SLACK_SALT_KEY_CHANNEL_ID` from the env. If
    either is missing, returns a zeroed stats dict and logs `skipped=1` —
    the rest of the sync continues normally so a missing token never
    breaks the cron job.

    Per record:
      * find `easebuzz_onboarding` row via merchants.mid → merchants.id
      * skip if no onboarding row exists (the merchant isn't on the
        dashboard yet — the next Submerchant sync will seed it, and
        we'll catch the salt&key on the following run)
      * skip if source = 'dashboard' (respect manual edits, matches the
        existing protection from sheets-side updates)
      * UPDATE salt_key_receipt to the header date (ISO text), and refresh
        salt_key_from_kickstart using existing helper.

    `backfill=True` walks the entire channel (oldest=0) instead of the
    lookback window. Used by `run_backfill`.
    """
    from datetime import date  # noqa: F401 - re-imported for type hint clarity

    stats = {
        "skipped": 0,                # 1 if env missing, else 0
        "records_seen": 0,
        "updated": 0,
        "no_onboarding_row": 0,
        "dashboard_protected": 0,
        "sheet_protected": 0,        # source='sheet' rows are never touched
        "no_merchant_mid": 0,
        "errors": 0,
    }

    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = os.environ.get("SLACK_SALT_KEY_CHANNEL_ID", "").strip()
    if not token or not channel:
        print("slack sync: SLACK_BOT_TOKEN or SLACK_SALT_KEY_CHANNEL_ID empty — skipped")
        stats["skipped"] = 1
        return stats

    client = slack_io.build_slack_client(token)
    if client is None:
        stats["skipped"] = 1
        return stats

    if backfill:
        since = datetime(1970, 1, 2, tzinfo=timezone.utc)
    else:
        days = lookback_days if lookback_days and lookback_days > 0 else _slack_lookback_days()
        since = datetime.now(timezone.utc) - timedelta(days=days)

    try:
        records = list(slack_io.fetch_salt_key_records(
            client, channel, since=since, fetch_permalinks=False,
        ))
    except Exception as e:
        print(f"slack sync: fetch failed ({type(e).__name__}: {e}) — skipping Slack step")
        stats["errors"] = 1
        return stats

    stats["records_seen"] = len(records)
    # Read holidays once outside the loop — file IO is cheap but the
    # for-loop iterates ~hundreds of records.
    holidays_cache = _load_holidays()

    for rec in records:
        cur.execute(
            """
            SELECT e.id, e.source
            FROM easebuzz_onboarding e
            JOIN merchants m ON m.id = e.merchant_id
            WHERE m.mid = %s
            LIMIT 1
            """,
            (rec.mid,),
        )
        row = cur.fetchone()
        if not row:
            # Could be because the merchant isn't in `merchants` yet, or
            # they're there but have no onboarding row. Lump both into
            # one bucket — the next Submerchant sync handles either.
            cur.execute("SELECT 1 FROM merchants WHERE mid = %s LIMIT 1", (rec.mid,))
            if cur.fetchone() is None:
                stats["no_merchant_mid"] += 1
            else:
                stats["no_onboarding_row"] += 1
            continue

        # Pull the full date context (and current status) once so we
        # can compute every derived column consistently for both seeded
        # and sheet paths.
        cur.execute(
            "SELECT kickstart_date, docs_received_date, "
            "       date_email_sent_to_eb, salt_key_receipt, onboarding_status "
            "FROM easebuzz_onboarding WHERE id = %s",
            (row["id"],),
        )
        row_ctx = cur.fetchone() or {}
        existing_sk = (row_ctx.get("salt_key_receipt") or "").strip()
        existing_status = (row_ctx.get("onboarding_status") or "").strip()

        # Field-level protection (option A++): the only thing Slack
        # owns is `salt_key_receipt`. If that specific field is already
        # non-blank on a non-seeded row, Ops set it manually — protected.
        # If it's blank, fill it regardless of which `source` the row
        # carries. This way a dashboard row whose user edited the
        # kickstart but never touched salt_key still gets its salt_key
        # auto-filled when Easebuzz posts the batch in Slack.
        if row["source"] != "seeded" and existing_sk:
            stats[f"{row['source']}_protected"] = (
                stats.get(f"{row['source']}_protected", 0) + 1
            )
            continue

        sk_new = rec.salt_key_date_text
        ks_text   = row_ctx.get("kickstart_date")
        docs_text = row_ctx.get("docs_received_date")
        email_text = row_ctx.get("date_email_sent_to_eb")

        new_eb_days = compute_business_days(email_text, sk_new, holidays_cache)
        new_docs_sk = compute_days_kickstart_to_salt_key(docs_text, sk_new)
        raw_ks_sk = compute_days_kickstart_to_salt_key(ks_text, sk_new)
        new_ks_sk: str | None
        if raw_ks_sk is not None and new_eb_days is not None:
            try:
                new_ks_sk = str(max(0, int(raw_ks_sk) - int(new_eb_days)))
            except ValueError:
                new_ks_sk = raw_ks_sk
        else:
            new_ks_sk = raw_ks_sk

        if row["source"] == "seeded":
            # Seeded rows: full auto — also flip onboarding_status based
            # on the kickstart+salt_key endpoint pair (the rule the
            # normalize step uses).
            new_status = _seeded_status(ks_text, sk_new)
            cur.execute(
                """
                UPDATE easebuzz_onboarding
                SET salt_key_receipt        = %s,
                    salt_key_from_kickstart = COALESCE(%s, salt_key_from_kickstart),
                    salt_key_from_docs_recd = COALESCE(%s, salt_key_from_docs_recd),
                    time_taken_by_eb        = COALESCE(%s, time_taken_by_eb),
                    onboarding_status       = %s,
                    last_synced_at = now(),
                    updated_at     = now()
                WHERE id = %s
                  AND (salt_key_receipt IS DISTINCT FROM %s)
                """,
                (sk_new, new_ks_sk, new_docs_sk, new_eb_days, new_status,
                 row["id"], sk_new),
            )
            if cur.rowcount > 0:
                stats["updated"] += 1
        else:
            # Non-seeded row (sheet OR dashboard) with previously-blank
            # salt_key. Fill it from Slack and recompute the derived
            # day-count columns inline.
            #
            # Status escalation: when both endpoints (kickstart +
            # salt&key) are now present, set onboarding_status='Yes'.
            # But never demote — if the row already says 'Yes' or
            # 'Live' (Live is a downstream state past Yes), leave it
            # alone. Status with blank/'No'/typo-variant gets escalated.
            auto_status = _seeded_status(ks_text, sk_new)
            new_status_to_apply: str | None = None
            if (auto_status == "Yes"
                    and existing_status.lower() not in ("live", "yes")):
                new_status_to_apply = "Yes"

            cur.execute(
                """
                UPDATE easebuzz_onboarding
                SET salt_key_receipt        = %s,
                    salt_key_from_kickstart = COALESCE(%s, salt_key_from_kickstart),
                    salt_key_from_docs_recd = COALESCE(%s, salt_key_from_docs_recd),
                    time_taken_by_eb        = COALESCE(%s, time_taken_by_eb),
                    onboarding_status       = COALESCE(%s, onboarding_status),
                    last_synced_at = now(),
                    updated_at     = now()
                WHERE id = %s
                  AND (salt_key_receipt IS NULL OR TRIM(salt_key_receipt) = '')
                """,
                (sk_new, new_ks_sk, new_docs_sk, new_eb_days,
                 new_status_to_apply, row["id"]),
            )
            if cur.rowcount > 0:
                stats["updated"] += 1
                # Per-source counter so the log line shows which kind of
                # row got filled (sheet_filled vs dashboard_filled).
                key = f"{row['source']}_filled"
                stats[key] = stats.get(key, 0) + 1
                if new_status_to_apply:
                    stats["status_escalated"] = (
                        stats.get("status_escalated", 0) + 1
                    )

    return stats


def _seed_onboarding_for_new_merchants(cur) -> dict[str, int]:
    """Auto-create dashboard onboarding rows for newly-added Submerchant MIDs.

    A merchant gets seeded iff ALL of:
      (a) has no matching `easebuzz_onboarding` row (by normalized name)
      (b) has a non-blank Gokwik KYC complete date
      (c) that date parses to something AFTER the seed cutoff (default
          2026-05-05 — anything on/before is already in the backfill)

    Seeded row gets:
      * docs_received_date = kyc_completed_by_ops = date_email_sent_to_eb
            = Gokwik KYC complete date (raw text from the sheet)
      * kickstart_date  ← Kickoff API for the batched date range; blank when
                          the API has no row for this merchant
      * salt_key_receipt ← still pending a separate API; left blank for now
      * source = 'seeded' (so the dashboard shows the "Needs review" badge)
    """
    cutoff = _kyc_seed_cutoff()

    cur.execute(
        """
        SELECT m.id, m.mid, m.merchant_name, m.name_normalized,
               m.gokwik_kyc_complete_date, m.merchant_size
        FROM merchants m
        LEFT JOIN easebuzz_onboarding e
            ON e.name_normalized = m.name_normalized
        WHERE m.name_normalized IS NOT NULL
          AND m.name_normalized <> ''
          AND e.id IS NULL
        """
    )
    candidates = cur.fetchall()

    stats = {
        "seeded": 0,
        "skipped_blank_kyc": 0,
        "skipped_pre_cutoff": 0,
        "skipped_unparseable_kyc": 0,
        "skipped_name_collision": 0,
        "kickoff_api_matched": 0,
    }

    # First pass: collect rows that pass the gates so we know the date range
    # we need from the Kickoff API. Each entry: (merchant_record, kyc_raw, kyc_date).
    seedable: list[tuple[dict, str, "date"]] = []
    for c in candidates:
        kyc_raw = (c["gokwik_kyc_complete_date"] or "").strip()
        if not kyc_raw:
            stats["skipped_blank_kyc"] += 1
            continue
        kyc_date = _parse_sheet_date(kyc_raw)
        if kyc_date is None:
            stats["skipped_unparseable_kyc"] += 1
            continue
        if kyc_date <= cutoff:
            stats["skipped_pre_cutoff"] += 1
            continue
        seedable.append((c, kyc_raw, kyc_date))

    if not seedable:
        return stats

    # Batched Kickoff API call. Pad ±14 days because the merchant's recorded
    # kickoff date may sit a few days before/after their KYC complete date.
    min_d = min(d for _, _, d in seedable)
    max_d = max(d for _, _, d in seedable)
    api_start = min_d - timedelta(days=14)
    api_end   = max_d + timedelta(days=14)
    kickoff_lookup = api_client.fetch_kickoff_data(api_start, api_end)
    loose_lookup = _loose_kickoff_lookup(kickoff_lookup)

    for c, kyc_raw, _ in seedable:
        info = _lookup_kickoff(kickoff_lookup, loose_lookup, c["name_normalized"])
        kickstart_date = info.get("kickoff") or None
        if kickstart_date and c["name_normalized"] not in kickoff_lookup:
            # Recovered via the suffix-stripping fallback — surface this
            # in stats so we know how many merchants the loose match
            # rescued from blank-kickstart limbo.
            stats["kickoff_api_loose_matched"] = (
                stats.get("kickoff_api_loose_matched", 0) + 1
            )
        kickstart_parsed = _parse_sheet_date(kickstart_date or "")
        if kickstart_date:
            stats["kickoff_api_matched"] += 1
        salt_key_receipt = None  # not provided by this API

        onboarding_status = _seeded_status(kickstart_date, salt_key_receipt)
        cur.execute(
            """
            INSERT INTO easebuzz_onboarding (
                merchant_id, merchant_name, name_normalized, merchant_size,
                onboarding_status,
                docs_received_date, kyc_completed_by_ops, date_email_sent_to_eb,
                kickstart_date, kickstart_date_parsed, salt_key_receipt,
                source, last_synced_at, created_at, updated_at
            )
            VALUES (
                %s, %s, %s, %s,
                %s,
                %s, %s, %s,
                %s, %s, %s,
                'seeded', now(), now(), now()
            )
            ON CONFLICT (name_normalized) DO NOTHING
            RETURNING id
            """,
            (
                c["id"], c["merchant_name"], c["name_normalized"],
                c.get("merchant_size"),
                onboarding_status,
                kyc_raw, kyc_raw, kyc_raw,
                kickstart_date, kickstart_parsed, salt_key_receipt,
            ),
        )
        # If a row with the same normalized name already existed (race with a
        # parallel backfill, or two merchants normalizing to the same key),
        # RETURNING yields no row. Count those so ops can investigate when
        # the number is unexpectedly large.
        if cur.fetchone() is None:
            stats["skipped_name_collision"] += 1
        else:
            stats["seeded"] += 1
    return stats


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
def _resolve_sync_url() -> str | None:
    return (
        os.environ.get("SYNC_DATABASE_URL")
        or os.environ.get("DATABASE_URL", "").replace("+asyncpg", "")
        or None
    )


def refetch_missing_kickstarts() -> dict[str, Any]:
    """Lightweight hourly job: re-call the Kickoff API for every seeded row
    whose `kickstart_date` is still blank, and fill in any that the API now
    has data for.

    Why this exists separately from the daily sync:
      * The Mintdash Kickoff API is populated asynchronously by another
        team — there's a lag between a merchant being seeded (KYC complete
        date assigned by Ops) and the Kickoff API getting their kickoff date.
      * The daily 04:00 sync only calls the Kickoff API at SEED time for
        brand-new merchants. Rows that were seeded yesterday with a blank
        kickstart never re-try otherwise.
      * Running hourly closes the gap so the dashboard reflects newly-
        assigned kickoffs within an hour of them appearing in Mintdash,
        which then auto-flips `onboarding_status` to Yes (via the
        normalize step) once salt&key also arrives.

    Steps:
      1. Find seeded rows where kickstart_date is NULL/blank.
      2. Compute the date window covering those rows' KYC dates (with ±14d
         padding, matching the seed-time window).
      3. Hit api_client.fetch_kickoff_data() once for that window.
      4. For each match by `name_normalized`, UPDATE kickstart_date +
         kickstart_date_parsed.
      5. Run _normalize_seeded_status() so derived columns + status flip
         immediately in the same transaction.

    Returns a stats dict; the CLI's main() prints it.
    """
    url = _resolve_sync_url()
    if not url:
        raise RuntimeError("DATABASE_URL / SYNC_DATABASE_URL not set")

    started_at = datetime.now(timezone.utc)
    stats: dict[str, Any] = {
        "started_at": started_at.isoformat(),
        "candidates": 0,
        "api_window_start": None,
        "api_window_end": None,
        "api_rows_returned": 0,
        "matched": 0,
        "updated": 0,
        "normalized": 0,
        "skipped_unparseable_kyc": 0,
    }

    with psycopg.connect(url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # Field-level protection rule: refetch kickstart for ANY row
            # whose kickstart_date is currently blank — including
            # dashboard rows (whose user edited a different field but
            # never set kickstart) and sheet rows (whose Easebuzz tab
            # didn't have kickstart yet). The UPDATE below additionally
            # checks `kickstart_date IS NULL` in its WHERE clause, so we
            # can never overwrite a value that was set between our
            # SELECT and our UPDATE.
            cur.execute(
                """
                SELECT e.id, e.name_normalized, e.kyc_completed_by_ops, e.source
                FROM easebuzz_onboarding e
                WHERE (e.kickstart_date IS NULL OR TRIM(e.kickstart_date) = '')
                """,
            )
            candidates = cur.fetchall()
            stats["candidates"] = len(candidates)
            if not candidates:
                return stats

            # Compute the API window. Parse each KYC date, take min/max, pad
            # by 14 days. Unparseable rows are skipped here but their row id
            # is still queried later (in case other rows match in the window).
            kyc_dates: list[date] = []
            for c in candidates:
                d = _parse_sheet_date(c.get("kyc_completed_by_ops") or "")
                if d is not None:
                    kyc_dates.append(d)
                else:
                    stats["skipped_unparseable_kyc"] += 1

            if not kyc_dates:
                # No usable KYC dates — can't size the window. Bail quietly.
                return stats

            # Clamp the API window so a single typo'd KYC date (e.g. an
            # old date entered by accident) can't drag the request range
            # across years. We center on today's date and cap to 180 days
            # in either direction — generous enough to cover real lag in
            # Mintdash, tight enough to avoid runaway API calls.
            today = date.today()
            lo = max(min(kyc_dates) - timedelta(days=14), today - timedelta(days=180))
            hi = min(max(kyc_dates) + timedelta(days=14), today + timedelta(days=60))
            window_start, window_end = lo, hi
            stats["api_window_start"] = window_start.isoformat()
            stats["api_window_end"]   = window_end.isoformat()

            try:
                kickoff_lookup = api_client.fetch_kickoff_data(window_start, window_end)
            except Exception as e:
                print(f"refetch_kickstarts: API call failed "
                      f"({type(e).__name__}: {e}) — bailing")
                return stats
            stats["api_rows_returned"] = len(kickoff_lookup)
            if not kickoff_lookup:
                return stats

            loose_lookup = _loose_kickoff_lookup(kickoff_lookup)

            cur.execute("SAVEPOINT refetch")
            try:
                for c in candidates:
                    info = _lookup_kickoff(
                        kickoff_lookup, loose_lookup, c["name_normalized"],
                    )
                    if not info or not info.get("kickoff"):
                        continue
                    if c["name_normalized"] not in kickoff_lookup:
                        # Match came from the loose fallback — count
                        # it separately so we can see the bug's impact.
                        stats["loose_matched"] = stats.get("loose_matched", 0) + 1
                    stats["matched"] += 1
                    new_kickstart      = info["kickoff"]
                    new_kickstart_parsed = _parse_sheet_date(new_kickstart)
                    cur.execute(
                        """
                        UPDATE easebuzz_onboarding
                        SET kickstart_date        = %s,
                            kickstart_date_parsed = %s,
                            last_synced_at        = now(),
                            updated_at            = now()
                        WHERE id = %s
                          AND (kickstart_date IS NULL OR TRIM(kickstart_date) = '')
                        """,
                        (new_kickstart, new_kickstart_parsed, c["id"]),
                    )
                    if cur.rowcount > 0:
                        stats["updated"] += 1

                # If we filled in any kickstarts, also do a wider Slack
                # sweep. The reason: a merchant whose KYC date landed late
                # might have had a salt&key batch posted to Slack older
                # than the default lookback. We size the lookback to cover
                # the OLDEST KYC date among the rows just updated, plus a
                # 7-day buffer — NOT the full channel history (which would
                # rate-limit and burn Slack credits every single hour for
                # the same merchants).
                if stats["updated"] > 0:
                    # The updated rows are a subset of `candidates`; pick the
                    # oldest KYC date among them to size the Slack lookback.
                    matched_kyc_dates = []
                    for c in candidates:
                        info = kickoff_lookup.get(c["name_normalized"])
                        if info and info.get("kickoff"):
                            d = _parse_sheet_date(c.get("kyc_completed_by_ops") or "")
                            if d is not None:
                                matched_kyc_dates.append(d)
                    today = date.today()
                    if matched_kyc_dates:
                        oldest = min(matched_kyc_dates)
                        lookback_days = max(
                            _slack_lookback_days(),
                            (today - oldest).days + 7,
                        )
                    else:
                        lookback_days = _slack_lookback_days()
                    # Cap at 365 days so a typo'd 2020 KYC date can't drag
                    # the lookback to span 6 years and rate-limit Slack.
                    lookback_days = min(lookback_days, 365)
                    slack_stats = sync_salt_keys_from_slack(
                        cur, lookback_days=lookback_days,
                    )
                    stats["slack_lookback_days"] = lookback_days
                    for k, v in slack_stats.items():
                        stats[f"slack_{k}"] = v

                # Recompute every seeded row's derived columns + status.
                # Runs after both kickstart + salt&key updates so a row
                # that picked up both this hour flips to Yes in the same
                # transaction.
                stats["normalized"] = _normalize_seeded_status(cur)
                cur.execute("RELEASE SAVEPOINT refetch")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT refetch")
                raise

        conn.commit()
    return stats


def _open_run(cur, started_at: datetime, triggered_by: str) -> str:
    cur.execute(
        """
        INSERT INTO sync_runs (started_at, status, triggered_by)
        VALUES (%s, 'running', %s) RETURNING id
        """,
        (started_at, triggered_by),
    )
    return cur.fetchone()["id"]


def _close_run_ok(cur, run_id: str, stats: dict[str, Any]) -> None:
    cur.execute(
        """
        UPDATE sync_runs SET
            finished_at = now(),
            status = 'success',
            gokwik_rows_seen = %(gokwik_rows_seen)s,
            gokwik_new_merchants = %(gokwik_new_merchants)s,
            gokwik_updated_merchants = %(gokwik_updated_merchants)s,
            easebuzz_rows_seen = %(easebuzz_rows_seen)s,
            easebuzz_new_rows = %(easebuzz_new_rows)s,
            easebuzz_updated_rows = %(easebuzz_updated_rows)s,
            easebuzz_linked_rows = %(easebuzz_linked_rows)s
        WHERE id = %(run_id)s
        """,
        {**{k: v for k, v in stats.items() if isinstance(v, int)}, "run_id": run_id},
    )


def _close_run_failed(cur, run_id: str, err: str) -> None:
    cur.execute(
        "UPDATE sync_runs SET status='failed', finished_at=now(), error=%s WHERE id=%s",
        (err, run_id),
    )


def _empty_stats(started_at: datetime) -> dict[str, Any]:
    return {
        "started_at": started_at,
        "gokwik_rows_seen": 0,
        "gokwik_new_merchants": 0,
        "gokwik_updated_merchants": 0,
        "easebuzz_rows_seen": 0,
        "easebuzz_new_rows": 0,
        "easebuzz_updated_rows": 0,
        "easebuzz_linked_rows": 0,
        "seeded_new_rows": 0,
        "skipped_blank_kyc": 0,
        "skipped_pre_cutoff": 0,
        "skipped_unparseable_kyc": 0,
        "skipped_name_collision": 0,
        "kickoff_api_matched": 0,
        # Slack salt&key sync — populated by sync_salt_keys_from_slack()
        "slack_skipped": 0,
        "slack_records_seen": 0,
        "slack_updated": 0,
        "slack_no_onboarding_row": 0,
        "slack_dashboard_protected": 0,
        "slack_no_merchant_mid": 0,
        "slack_errors": 0,
    }


def _merge_slack_stats(stats: dict[str, Any], slack_stats: dict[str, int]) -> None:
    """Copy slack_io stats into the main stats dict with `slack_` prefix."""
    for k, v in slack_stats.items():
        stats[f"slack_{k}"] = v


def _normalize_seeded_status(cur) -> int:
    """For every source='seeded' row, force the derived columns to stay in
    sync with the underlying date inputs. Returns rows-changed count.

    Derived rules (only applied to seeded rows; sheet/dashboard rows are
    skipped so their hand-entered values are preserved):
      * onboarding_status      = 'Yes' iff kickstart_date AND salt_key_receipt
      * salt_key_from_docs_recd = |salt_key - docs_received_date|  (days)
      * salt_key_from_kickstart = |salt_key - kickstart_date|       (days)

    Idempotent. Runs at the end of every sync/backfill so rows whose
    kickstart arrives on a different cycle than salt&key still end up with
    every derived column filled in as soon as both endpoints are present.
    """
    cur.execute(
        """
        SELECT id, kickstart_date, docs_received_date, kyc_completed_by_ops,
               date_email_sent_to_eb, salt_key_receipt,
               onboarding_status, time_taken_by_eb,
               salt_key_from_docs_recd, salt_key_from_kickstart
        FROM easebuzz_onboarding
        WHERE source = 'seeded'
        """,
    )
    rows = cur.fetchall()

    holidays = _load_holidays()

    changed = 0
    for r in rows:
        # Canonicalize all date columns to the sheet's `dd-MMM-yy` form so
        # the row reads consistently across columns (mixing ISO from feeds
        # with dd-MMM-yy from the sheet looks wrong).
        new_kickstart = _canonical_sheet_date(r.get("kickstart_date"))
        new_docs      = _canonical_sheet_date(r.get("docs_received_date"))
        new_kyc       = _canonical_sheet_date(r.get("kyc_completed_by_ops"))
        new_email_eb  = _canonical_sheet_date(r.get("date_email_sent_to_eb"))
        new_salt_key  = _canonical_sheet_date(r.get("salt_key_receipt"))

        kickstart_for_calc = (new_kickstart or "").strip() or None
        docs_for_calc      = (new_docs or "").strip() or None
        email_eb_for_calc  = (new_email_eb or "").strip() or None
        salt_key_for_calc  = (new_salt_key or "").strip() or None

        new_status   = _seeded_status(kickstart_for_calc, salt_key_for_calc)
        new_docs_sk  = compute_days_kickstart_to_salt_key(docs_for_calc, salt_key_for_calc)
        # EB days = business days between Email-to-EB and Salt&Key receipt,
        # excluding weekends + holidays from backend/holidays.txt.
        new_eb_days  = compute_business_days(email_eb_for_calc, salt_key_for_calc, holidays)
        # K→S&K = calendar days (kickstart → salt&key) MINUS EB days,
        # clamped to >= 0. Meaning: the slice of the end-to-end window that
        # is NOT attributable to EB's own processing time (i.e. the upstream
        # gap from kickstart through to the moment EB picked up the
        # request). The clamp catches data orderings where EB's email-to-key
        # span starts before the recorded kickstart date — surfacing a
        # negative number there is more confusing than informative.
        raw_ks_sk_str = compute_days_kickstart_to_salt_key(kickstart_for_calc, salt_key_for_calc)
        if raw_ks_sk_str is not None and new_eb_days is not None:
            try:
                new_ks_sk = str(max(0, int(raw_ks_sk_str) - int(new_eb_days)))
            except ValueError:
                new_ks_sk = raw_ks_sk_str
        else:
            new_ks_sk = raw_ks_sk_str

        # COALESCE-style: never overwrite an existing computed value with None
        # (would happen if salt_key_receipt gets blanked, but Slack only sets
        # it, never clears it — still, be safe).
        cur_docs_sk  = r.get("salt_key_from_docs_recd")
        cur_ks_sk    = r.get("salt_key_from_kickstart")
        cur_eb_days  = r.get("time_taken_by_eb")
        target_docs_sk = new_docs_sk if new_docs_sk is not None else cur_docs_sk
        target_ks_sk   = new_ks_sk   if new_ks_sk   is not None else cur_ks_sk
        target_eb_days = new_eb_days if new_eb_days is not None else cur_eb_days

        if (r.get("kickstart_date")          == new_kickstart
            and r.get("docs_received_date")  == new_docs
            and r.get("kyc_completed_by_ops") == new_kyc
            and r.get("date_email_sent_to_eb") == new_email_eb
            and r.get("salt_key_receipt")    == new_salt_key
            and r.get("onboarding_status")   == new_status
            and cur_docs_sk                  == target_docs_sk
            and cur_ks_sk                    == target_ks_sk
            and cur_eb_days                  == target_eb_days):
            continue

        cur.execute(
            """
            UPDATE easebuzz_onboarding
            SET kickstart_date          = %s,
                docs_received_date      = %s,
                kyc_completed_by_ops    = %s,
                date_email_sent_to_eb   = %s,
                salt_key_receipt        = %s,
                onboarding_status       = %s,
                time_taken_by_eb        = %s,
                salt_key_from_docs_recd = %s,
                salt_key_from_kickstart = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (new_kickstart, new_docs, new_kyc, new_email_eb, new_salt_key,
             new_status, target_eb_days, target_docs_sk, target_ks_sk, r["id"]),
        )
        changed += 1
    return changed


def run_sync(*, triggered_by: str = "manual") -> dict[str, Any]:
    """Regular weekly run: Submerchant only, seed new rows.

    Transaction layout:
      * One outer connection / transaction for the whole run.
      * SAVEPOINT `run_row` wraps the audit-row INSERT.
      * SAVEPOINT `upserts` wraps the merchant + seeding writes.
      * On success: release upserts, update audit row to success, COMMIT.
      * On upsert failure: rollback to upserts (preserves audit row),
        mark audit row as failed, COMMIT, re-raise.

    Net effect: every run leaves exactly one row in `sync_runs` in either
    `success` or `failed` state. Previously the two separate commits could
    leave orphan rows on partial failure.
    """
    url = _resolve_sync_url()
    if not url:
        raise RuntimeError("DATABASE_URL / SYNC_DATABASE_URL not set")

    started_at = datetime.now(timezone.utc)
    svc = sheets_io.build_sheets_service()
    gokwik_rows = sheets_io.fetch_gokwik_merchants(svc)

    stats = _empty_stats(started_at)
    stats["gokwik_rows_seen"] = len(gokwik_rows)
    run_id: str | None = None

    with psycopg.connect(url, row_factory=dict_row) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT run_row")
                run_id = _open_run(cur, started_at, triggered_by)
                cur.execute("RELEASE SAVEPOINT run_row")

                cur.execute("SAVEPOINT upserts")
                try:
                    for rec in gokwik_rows:
                        op = _merchant_upsert(cur, rec)
                        if op == "inserted":
                            stats["gokwik_new_merchants"] += 1
                        elif op == "updated":
                            stats["gokwik_updated_merchants"] += 1

                    seed_stats = _seed_onboarding_for_new_merchants(cur)
                    stats["seeded_new_rows"]         = seed_stats["seeded"]
                    stats["skipped_blank_kyc"]       = seed_stats["skipped_blank_kyc"]
                    stats["skipped_pre_cutoff"]      = seed_stats["skipped_pre_cutoff"]
                    stats["skipped_unparseable_kyc"] = seed_stats["skipped_unparseable_kyc"]
                    stats["skipped_name_collision"]  = seed_stats["skipped_name_collision"]
                    stats["kickoff_api_matched"]     = seed_stats["kickoff_api_matched"]
                    stats["easebuzz_new_rows"]       = seed_stats["seeded"]
                    stats["easebuzz_linked_rows"]    = seed_stats["seeded"]

                    # Slack salt&key sync runs INSIDE the same savepoint so a
                    # network blip mid-channel-walk rolls back atomically with
                    # the rest of the run. The fetch itself short-circuits on
                    # token/channel misconfig and just records skipped=1.
                    slack_stats = sync_salt_keys_from_slack(cur)
                    _merge_slack_stats(stats, slack_stats)

                    # Final pass: keep every seeded row's onboarding_status
                    # in lock-step with (kickstart_date, salt_key_receipt).
                    # Sheet/dashboard rows are untouched by this step.
                    stats["seeded_status_renormalized"] = _normalize_seeded_status(cur)

                    cur.execute("RELEASE SAVEPOINT upserts")
                except Exception:
                    # Roll the data work back but KEEP the audit row so
                    # /api/sync/last reflects the failure.
                    cur.execute("ROLLBACK TO SAVEPOINT upserts")
                    raise

                _close_run_ok(cur, run_id, stats)
            conn.commit()
        except Exception as e:
            # Best-effort failure record. If even this UPDATE blows up (e.g.
            # connection died), the reaper at next startup will mark the row
            # as `failed` with "stale run >2h".
            if run_id is not None:
                try:
                    with conn.cursor() as cur:
                        _close_run_failed(cur, run_id, str(e))
                    conn.commit()
                except Exception:
                    pass
            raise

    stats["run_id"] = str(run_id) if run_id else ""
    return stats


def run_backfill(*, triggered_by: str = "manual", force: bool = False) -> dict[str, Any]:
    """One-time historical import: full Submerchant + full Easebuzz tab.

    Safety:
      Refuses to run if any row in `easebuzz_onboarding` already carries
      `source='dashboard'` — running backfill on top of dashboard edits
      would silently overwrite them via the ON CONFLICT DO UPDATE on the
      Easebuzz upsert path. Pass `force=True` (CLI: `--force`) only when
      you've audited the impact yourself.

    Transaction layout: same as `run_sync` — see that docstring.
    """
    url = _resolve_sync_url()
    if not url:
        raise RuntimeError("DATABASE_URL / SYNC_DATABASE_URL not set")

    # Cheap pre-flight check on a separate connection — keeps the safety
    # decision well away from the transaction that will do the writing.
    if not force:
        with psycopg.connect(url, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM easebuzz_onboarding WHERE source = 'dashboard'"
            )
            n = cur.fetchone()["n"]
        if n > 0:
            raise RuntimeError(
                f"Backfill refused: {n} dashboard-edited rows exist; "
                f"use --force to override."
            )

    started_at = datetime.now(timezone.utc)
    svc = sheets_io.build_sheets_service()
    gokwik_rows   = sheets_io.fetch_gokwik_merchants(svc)
    easebuzz_rows = sheets_io.fetch_easebuzz_onboarding(svc)

    stats = _empty_stats(started_at)
    stats["gokwik_rows_seen"]   = len(gokwik_rows)
    stats["easebuzz_rows_seen"] = len(easebuzz_rows)
    run_id: str | None = None

    with psycopg.connect(url, row_factory=dict_row) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT run_row")
                run_id = _open_run(cur, started_at, triggered_by + ":backfill")
                cur.execute("RELEASE SAVEPOINT run_row")

                cur.execute("SAVEPOINT upserts")
                try:
                    for rec in gokwik_rows:
                        op = _merchant_upsert(cur, rec)
                        if op == "inserted":
                            stats["gokwik_new_merchants"] += 1
                        elif op == "updated":
                            stats["gokwik_updated_merchants"] += 1
                    for rec in easebuzz_rows:
                        op, linked = _easebuzz_upsert_from_sheet(cur, rec)
                        if op == "inserted":
                            stats["easebuzz_new_rows"] += 1
                        elif op == "updated":
                            stats["easebuzz_updated_rows"] += 1
                        if linked:
                            stats["easebuzz_linked_rows"] += 1

                    # After importing the historical Easebuzz rows, seed any
                    # post-cutoff Submerchant MIDs that still don't have an
                    # onboarding row.
                    seed_stats = _seed_onboarding_for_new_merchants(cur)
                    stats["seeded_new_rows"]         = seed_stats["seeded"]
                    stats["skipped_blank_kyc"]       = seed_stats["skipped_blank_kyc"]
                    stats["skipped_pre_cutoff"]      = seed_stats["skipped_pre_cutoff"]
                    stats["skipped_unparseable_kyc"] = seed_stats["skipped_unparseable_kyc"]
                    stats["skipped_name_collision"]  = seed_stats["skipped_name_collision"]
                    stats["kickoff_api_matched"]     = seed_stats["kickoff_api_matched"]
                    stats["easebuzz_new_rows"]      += seed_stats["seeded"]

                    # Backfill walks the entire Slack channel — fills in
                    # historical salt&key dates for every dashboard row
                    # whose merchant MID appeared in any digest.
                    slack_stats = sync_salt_keys_from_slack(cur, backfill=True)
                    _merge_slack_stats(stats, slack_stats)

                    stats["seeded_status_renormalized"] = _normalize_seeded_status(cur)

                    cur.execute("RELEASE SAVEPOINT upserts")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT upserts")
                    raise

                _close_run_ok(cur, run_id, stats)
            conn.commit()
        except Exception as e:
            if run_id is not None:
                try:
                    with conn.cursor() as cur:
                        _close_run_failed(cur, run_id, str(e))
                    conn.commit()
                except Exception:
                    pass
            raise

    stats["run_id"] = str(run_id) if run_id else ""
    return stats


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true",
                        help="One-time historical import — pulls the full Easebuzz tab in addition to Submerchant.")
    parser.add_argument("--force", action="store_true",
                        help="Allow --backfill to run even when dashboard-edited rows exist. "
                             "Without this, backfill aborts to protect your manual edits.")
    parser.add_argument("--refetch-kickstarts", action="store_true",
                        help="Hourly mode — only re-hit the Kickoff API for seeded rows still "
                             "missing kickstart_date, fill in any new matches, and renormalize "
                             "derived columns. Doesn't touch the Submerchant tab or Slack.")
    parser.add_argument("--triggered-by", default="cron",
                        choices=["cron", "api", "manual"])
    args = parser.parse_args()

    if args.refetch_kickstarts:
        started = datetime.now(timezone.utc)
        print(f"[{started.isoformat()}] starting kickstart refetch "
              f"(trigger={args.triggered_by})")
        try:
            stats = refetch_missing_kickstarts()
        except RuntimeError as e:
            print(f"CONFIG ERROR: {e}", file=sys.stderr)
            return 1
        except Exception as e:
            traceback.print_exc()
            print(f"FAIL: {e}", file=sys.stderr)
            return 2
        print(f"REFETCH  candidates={stats['candidates']} "
              f"window=[{stats['api_window_start']}..{stats['api_window_end']}] "
              f"api_rows={stats['api_rows_returned']} "
              f"matched={stats['matched']} "
              f"loose_matched={stats.get('loose_matched', 0)} "
              f"updated={stats['updated']} "
              f"normalized={stats['normalized']} "
              f"skipped_unparseable_kyc={stats['skipped_unparseable_kyc']}")
        # The Slack backfill fallback only runs when ≥1 kickstart was
        # filled this round (see refetch_missing_kickstarts). Surface its
        # stats on the next line so the hourly log shows both phases.
        if stats.get("slack_records_seen") is not None:
            print(f"REFETCH-SLACK  records_seen={stats.get('slack_records_seen', 0)} "
                  f"updated={stats.get('slack_updated', 0)} "
                  f"no_onboarding_row={stats.get('slack_no_onboarding_row', 0)} "
                  f"no_merchant_mid={stats.get('slack_no_merchant_mid', 0)} "
                  f"sheet_protected={stats.get('slack_sheet_protected', 0)} "
                  f"dashboard_protected={stats.get('slack_dashboard_protected', 0)} "
                  f"errors={stats.get('slack_errors', 0)}")
        return 0

    mode = "backfill" if args.backfill else "sync"
    started = datetime.now(timezone.utc)
    print(f"[{started.isoformat()}] starting {mode} (trigger={args.triggered_by})")
    try:
        if args.backfill:
            stats = run_backfill(triggered_by=args.triggered_by, force=args.force)
        else:
            stats = run_sync(triggered_by=args.triggered_by)
    except RuntimeError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        traceback.print_exc()
        print(f"FAIL: {e}", file=sys.stderr)
        return 2

    seeded = stats.get("seeded_new_rows", 0)
    print(f"DONE  "
          f"gokwik (new={stats['gokwik_new_merchants']}, upd={stats['gokwik_updated_merchants']}) | "
          f"easebuzz from sheet (new={stats['easebuzz_new_rows'] - seeded}, "
          f"upd={stats['easebuzz_updated_rows']}) | "
          f"seeded={seeded} "
          f"kickoff_api_matched={stats.get('kickoff_api_matched', 0)} "
          f"skipped(blank={stats.get('skipped_blank_kyc', 0)}, "
          f"pre_cutoff={stats.get('skipped_pre_cutoff', 0)}, "
          f"unparseable={stats.get('skipped_unparseable_kyc', 0)}, "
          f"name_collision={stats.get('skipped_name_collision', 0)})")
    if stats.get("slack_skipped"):
        print("SLACK skipped (no token/channel configured)")
    else:
        print(f"SLACK  "
              f"records_seen={stats.get('slack_records_seen', 0)} "
              f"updated={stats.get('slack_updated', 0)} "
              f"sheet_filled={stats.get('slack_sheet_filled', 0)} "
              f"dashboard_filled={stats.get('slack_dashboard_filled', 0)} "
              f"status_escalated={stats.get('slack_status_escalated', 0)} "
              f"no_onboarding_row={stats.get('slack_no_onboarding_row', 0)} "
              f"no_merchant_mid={stats.get('slack_no_merchant_mid', 0)} "
              f"sheet_protected={stats.get('slack_sheet_protected', 0)} "
              f"dashboard_protected={stats.get('slack_dashboard_protected', 0)} "
              f"errors={stats.get('slack_errors', 0)}")
    print(f"STATUS  seeded_renormalized={stats.get('seeded_status_renormalized', 0)} "
          f"(only source='seeded' rows; sheet/dashboard untouched)")
    return 0
