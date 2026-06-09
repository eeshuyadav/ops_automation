"""
Production fetch + adaptive halving.

Combines:
  - The join-based MBQL from fetch_via_metabase.py — filters on
    unified_orders.created_at (matches QuickSight's dashboard semantics).
    Produces the full 16-column Ops_SALE_TRANSACTION schema.
  - The adaptive halving from fetch_for_validation.py — starts day-sized,
    splits windows on failure, unlimited retries at the floor.
  - The 6-merchant exclusion baked into the MBQL filter so those rows
    never leave Metabase.

Why this exists:
  fetch_via_metabase.py has the right semantics but fails on older
  partitions (reverse-proxy 504s on a day-sized query). fetch_for_validation.py
  survives timeouts but uses transactions_model_view.created_at — which
  differs from unified_orders.created_at when a payment is retried across
  a day boundary, producing a small but real ~0.2 % drift on older weeks.
  This file gets both correct.

Usage:
    python fetch_production_adaptive.py --start 2026-03-02 --end 2026-03-08 \\
        --out Ops_SALE_TRANSACTION_production_2026-03-02_2026-03-08.csv
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Iterable

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name("env"))
MB_URL = os.environ["METABASE_URL"].rstrip("/")
MB_KEY = os.environ["METABASE_API_KEY"]

# Trino-Prod table names as Metabase syncs them. Original SQL references the
# *views* (unified_orders_view, business_merchants_master_v2) but Metabase only
# syncs the gold-layer tables the views wrap — column-equivalent for our use.
TXN_TABLE       = "transactions_model_view"       # schema: gk_lakehouse_views
ORDERS_TABLE    = "unified_orders"                # schema: gk_lakehouse_gold
MERCHANTS_TABLE = "merchants_master"              # schema: gk_lakehouse_gold

REQUIRED_TABLES = (TXN_TABLE, ORDERS_TABLE, MERCHANTS_TABLE)

# Output schema (matches the manual Ops_SALE_TRANSACTION_*.csv format the
# downstream update_gmv_weekly.py expects). Each entry is (csv_header, src, col)
# where src is 't' (transactions), 'o' (unified_orders, also the join base),
# or 'm' (merchants_master).
OUTPUT_COLUMNS: list[tuple[str, str, str]] = [
    ("MERCHANT ID",          "o", "merchant_id"),
    ("MERCHANT NAME",        "m", "short_name"),
    ("ORDER ID",             "o", "id"),
    ("MOID",                 "o", "moid"),
    ("ORDER DATE",           "o", "created_at"),
    ("PAYMENT METHOD",       "o", "payment_method"),
    ("ORDER PAYMENT STATUS", "o", "payment_status"),
    ("BANK STATUS",          "t", "bank_status"),
    ("STATUS",               "t", "status"),
    ("FAILURE CODE",         "t", "failure_code"),
    ("RESPONSE CODE",        "t", "response_code"),
    ("PAYMENT ID",           "t", "payment_id"),
    ("CUST REF ID",          "t", "cust_ref_id"),
    ("USER AGENT",           "o", "user_agent"),
    ("payment_provider",     "t", "payment_provider"),
    ("TXN AMT",              "t", "amount"),
]


def _api_get(path: str):
    r = requests.get(f"{MB_URL}{path}", headers={"X-API-Key": MB_KEY}, timeout=60)
    r.raise_for_status()
    return r.json()


def discover() -> dict:
    """Walk every Metabase database and locate the 3 required tables + their fields."""
    print(f"Listing databases at {MB_URL} ...")
    resp = _api_get("/api/database")
    dbs = resp.get("data", resp) if isinstance(resp, dict) else resp

    found: dict[str, dict] = {}
    for db in dbs:
        if db.get("is_sample"):
            continue
        db_id, db_name = db["id"], db["name"]
        print(f"  db={db_name!r} id={db_id} engine={db.get('engine')}")
        try:
            meta = _api_get(f"/api/database/{db_id}/metadata")
        except requests.HTTPError as e:
            print(f"    skipped: {e}")
            continue
        for t in meta.get("tables", []):
            if t["name"] in REQUIRED_TABLES:
                fields = {f["name"]: f["id"] for f in t.get("fields", [])}
                found[t["name"]] = {
                    "db_id": db_id,
                    "db_name": db_name,
                    "table_id": t["id"],
                    "schema": t.get("schema"),
                    "fields": fields,
                }
                print(
                    f"    FOUND {t['name']} schema={t.get('schema')} "
                    f"table_id={t['id']} fields={len(fields)}"
                )

    missing = set(REQUIRED_TABLES) - set(found)
    if missing:
        raise SystemExit(f"\nTables not found in Metabase: {sorted(missing)}")

    db_ids = {t["db_id"] for t in found.values()}
    if len(db_ids) != 1:
        split = {name: f["db_name"] for name, f in found.items()}
        raise SystemExit(
            "Required tables span multiple Metabase databases — "
            "MBQL cannot join across them:\n"
            f"  {json.dumps(split, indent=2)}"
        )
    return found


def _field_ref(tables: dict, src_table: str, col: str):
    """Build an MBQL `["field", id, opts]` reference. src_table is 't', 'o', or 'm'.
    'o' is the join base (unified_orders, source-table); 't' and 'm' are aliases."""
    table_key = {"t": TXN_TABLE, "o": ORDERS_TABLE, "m": MERCHANTS_TABLE}[src_table]
    table = tables[table_key]
    if col not in table["fields"]:
        raise SystemExit(
            f"Column {col!r} not present on {table_key}. "
            f"Available: {sorted(table['fields'])}"
        )
    fid = table["fields"][col]
    if src_table == "o":
        return ["field", fid, None]
    return ["field", fid, {"join-alias": src_table}]

# Same 6-merchant set as update_gmv_weekly.EXCLUDED_MERCHANTS (keep in sync).
EXCLUDED_MERCHANTS: tuple[int, ...] = (6792, 3500, 3742, 2928, 13947, 9462)

HEADER = ",".join(f'"{c[0]}"' for c in OUTPUT_COLUMNS) + "\n"

MAX_BACKOFF_SEC = 300  # ceiling on per-attempt retry wait


def build_mbql_with_excludes(
    tables: dict,
    start: dt.datetime,
    end: dt.datetime,
    payment_provider: str = "easebuzz",
    bank_status: str | None = None,
    active_only: bool = False,  # default OFF — see experiment log
    merchant_whitelist: list[int] | None = None,
) -> dict:
    """Same as fetch_via_metabase.build_mbql but also excludes the 6 merchants
    via a `not-in` filter at MBQL level.

    Note on `active_only`: DEFAULT IS OFF.

    We attempted several filters to eliminate a +0.017 % residual (Idaho,
    Kilkaari, Surtisilk) on 2-8 Mar vs QS:

      no filter                                    +0.017 % (99/102 exact)
      is_active = True                             -0.027 % (95/102)
      is_auto_refund = False                       -0.256 % (87/102)
      (is_auto_refund = True AND status='refunded') -0.046 % (91/102)

    Every filter we tried made the diff worse than no filter — because
    QS's dashboard filter involves compound conditions on fields we can't
    reverse-engineer externally. Until the DB team tells us the exact
    dashboard SQL, the best-match setting is active_only=False (no filter),
    leaving the +0.017 % residual as the empirical floor."""
    db_id = next(iter({t["db_id"] for t in tables.values()}))
    selected = [_field_ref(tables, src, col) for _, src, col in OUTPUT_COLUMNS]

    clauses: list = [
        [
            "between",
            _field_ref(tables, "o", "created_at"),
            start.isoformat(),
            end.isoformat(),
        ],
        [
            "=",
            _field_ref(tables, "t", "payment_provider"),
            payment_provider,
        ],
        # MBQL `!=` with multiple values behaves as "not in" when you list
        # the values positionally. Equivalent to NOT IN (6792, 3500, ...).
        [
            "!=",
            _field_ref(tables, "o", "merchant_id"),
            *EXCLUDED_MERCHANTS,
        ],
    ]
    if merchant_whitelist:
        # Push the SDS-tracked merchant list (read from the GMV sheet) into
        # the MBQL filter — pulls only merchants we'll actually write to,
        # cuts fetch volume from ~2.6M rows to ~70k.
        clauses.append([
            "=",
            _field_ref(tables, "o", "merchant_id"),
            *merchant_whitelist,
        ])
    if bank_status:
        clauses.append(["=", _field_ref(tables, "t", "bank_status"), bank_status])
    if active_only:
        # NOTE: The QS filter IS NOT reproducible from column values on
        # transactions_model_view. Confirmed by pulling all 92 fields for
        # 2 rows QS drops + 2 rows QS keeps (same merchant, same day,
        # same is_active=False AND is_auto_refund=True signature): every
        # filter-relevant column has identical values between drops and
        # keeps. The differentiator lives either in SPICE-level dataset
        # filters, a dashboard-side calculated field, or a lookup/join
        # not exposed to Metabase. Only Ashish/DB team can surface it.
        #
        # This is kept as an opt-in knob for future use if the filter
        # is ever identified. Current best compound kept for reference:
        clauses.append([
            "or",
            ["=", _field_ref(tables, "t", "is_active"), True],
            ["=", _field_ref(tables, "t", "is_auto_refund"), False],
        ])

    return {
        "database": db_id,
        "type": "query",
        "query": {
            "source-table": tables[ORDERS_TABLE]["table_id"],
            "joins": [
                {
                    "alias": "t",
                    "source-table": tables[TXN_TABLE]["table_id"],
                    "strategy": "inner-join",
                    "fields": "none",
                    "condition": [
                        "=",
                        _field_ref(tables, "o", "order_number"),
                        _field_ref(tables, "t", "order_number"),
                    ],
                },
                {
                    "alias": "m",
                    "source-table": tables[MERCHANTS_TABLE]["table_id"],
                    "strategy": "inner-join",
                    "fields": "none",
                    "condition": [
                        "=",
                        _field_ref(tables, "o", "merchant_id"),
                        _field_ref(tables, "m", "id"),
                    ],
                },
            ],
            "filter": ["and", *clauses],
            "fields": selected,
        },
    }


def fetch_csv_once(mbql: dict) -> bytes:
    r = requests.post(
        f"{MB_URL}/api/dataset/csv",
        headers={"X-API-Key": MB_KEY},
        data={"query": json.dumps(mbql)},
        timeout=600,
        stream=False,
    )
    if r.status_code not in (200, 202):
        raise requests.HTTPError(f"HTTP {r.status_code}: {r.text[:300]}")
    return r.content


def strip_csv_header(body: bytes) -> bytes:
    if not body:
        return b""
    i = body.find(b"\n")
    return b"" if i == -1 else body[i + 1:]


TRANSIENT = (
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ConnectionError,
    requests.exceptions.ReadTimeout,
    requests.HTTPError,
)


def part_path(parts_dir: Path, start: dt.datetime, end: dt.datetime) -> Path:
    label = f"{start.strftime('%Y%m%dT%H%M')}_{end.strftime('%Y%m%dT%H%M')}"
    return parts_dir / f"{label}.csv"


def fetch_window_adaptive(
    start: dt.datetime,
    end: dt.datetime,
    tables: dict,
    parts_dir: Path,
    min_minutes: int,
    attempts_per_level: int,
    payment_provider: str,
    bank_status: str | None,
    merchant_whitelist: list[int] | None = None,
    active_only: bool = True,
    depth: int = 0,
) -> None:
    """Try the window. Split on repeated failure. At floor, retry forever."""
    pp = part_path(parts_dir, start, end)
    if pp.exists():
        kb = pp.stat().st_size / 1024
        print(f"  {pp.name}  cached ({kb:.1f} KB) — skipping", flush=True)
        return

    span_min = int((end - start).total_seconds() / 60) + 1
    label = f"{start.strftime('%Y-%m-%d %H:%M')}→{end.strftime('%H:%M')}  (~{span_min}m)"
    at_floor = span_min <= min_minutes

    mbql = build_mbql_with_excludes(
        tables, start, end, payment_provider, bank_status,
        merchant_whitelist=merchant_whitelist,
    )

    attempt = 0
    failures = 0
    while True:
        attempt += 1
        t0 = time.time()
        try:
            body = fetch_csv_once(mbql)
            dur = time.time() - t0
            data = strip_csv_header(body)
            rows = data.count(b"\n") if data else 0
            pp.write_bytes(data)
            print(f"  OK   {label}  {rows:>7,} rows  {dur:>5.1f}s  [depth={depth}, attempt {attempt}]",
                  flush=True)
            return
        except TRANSIENT as e:
            dur = time.time() - t0
            failures += 1
            kind = type(e).__name__
            msg = str(e)[:140]
            if at_floor or attempt < attempts_per_level:
                wait = min(MAX_BACKOFF_SEC, 2 ** min(attempt, 8))
                tag = "FLOOR" if at_floor else f"depth={depth}"
                print(f"  FAIL {label}  {dur:>5.1f}s  [{tag}, attempt {attempt}] "
                      f"{kind}: {msg} — backoff {wait}s", flush=True)
                time.sleep(wait)
                continue
            print(f"  SPLIT {label}  after {attempt} attempts — subdividing",
                  flush=True)
            break

    total_sec = int((end - start).total_seconds()) + 1
    mid = start + dt.timedelta(seconds=total_sec // 2)
    fetch_window_adaptive(
        start, mid - dt.timedelta(seconds=1),
        tables, parts_dir, min_minutes, attempts_per_level,
        payment_provider, bank_status,
        merchant_whitelist=merchant_whitelist, depth=depth + 1,
    )
    fetch_window_adaptive(
        mid, end,
        tables, parts_dir, min_minutes, attempts_per_level,
        payment_provider, bank_status,
        merchant_whitelist=merchant_whitelist, depth=depth + 1,
    )


def iter_days(start_d: dt.date, end_d: dt.date) -> Iterable[tuple[dt.datetime, dt.datetime]]:
    d = start_d
    while d <= end_d:
        yield (
            dt.datetime.combine(d, dt.time.min),
            dt.datetime.combine(d, dt.time(23, 59, 59)),
        )
        d += dt.timedelta(days=1)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--parts-dir", type=Path, default=None)
    p.add_argument("--min-minutes", type=int, default=22,
                   help="Floor for subdivision. Default 22 min.")
    p.add_argument("--attempts-per-level", type=int, default=3)
    p.add_argument("--payment-provider", default="easebuzz",
                   help="Baked into MBQL filter. Default 'easebuzz' (matches dashboard).")
    p.add_argument("--bank-status", default="any",
                   help="Optional bank_status filter. Default 'any'. Pass "
                        "'payment_successful' to only fetch successful txns.")
    p.add_argument("--sheet-id", default="1CPuJSbd4emdVzfUOpr7cyYE0FBLlqlYE3_OHQtQATcY",
                   help="Google Sheet ID for the merchant whitelist source. "
                        "Default = SDS MID enablement tracker "
                        "('Commercials MDR - PG\\'S' workbook).")
    p.add_argument("--gid", type=int, default=759157239,
                   help="Worksheet gid (default = 'Same Day Settlements Merchants' tab).")
    p.add_argument("--oauth-token", type=Path,
                   default=Path(__file__).with_name("oauth_token.json"),
                   help="OAuth token (chargeback-automation@gokwik.co) to read the "
                        "MID tracker. The tracker's owner only grants view to this user.")
    p.add_argument("--no-sheet-filter", action="store_true",
                   help="Disable the sheet-merchant whitelist; fetch ALL merchants. "
                        "Default behaviour pulls only the SDS-tracked merchants.")
    return p.parse_args()


_DATE_FORMATS = (
    "%d-%B-%Y",      # "4-May-2026" — full month name
    "%d-%b-%Y",      # "4-May-2026" / "16-Mar-2026" — abbreviated
    "%d-%m-%Y",      # "13-3-2024"
    "%Y-%m-%d",      # "2024-03-13"
    "%d %B %Y",      # "4 May 2026"
    "%d %b %Y",      # "4 Mar 2026"
    "%d/%m/%Y",      # "13/03/2024"
    "%d.%m.%Y",      # "13.03.2024"
)


def _parse_enable_date(s: str):
    """Parse a 'Date of Enabling' string into a date. Returns None if
    unparseable — caller should treat that as 'no date filter' (include the
    merchant) so a typo in the tracker doesn't accidentally hide them.

    Strips ordinal suffixes (1st/2nd/3rd/4th) before trying formats."""
    import datetime as _dt
    import re as _re
    if not s:
        return None
    s = s.strip()
    s = _re.sub(r'(\d)(st|nd|rd|th)\b', r'\1', s, flags=_re.IGNORECASE)
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def get_merchant_whitelist(
    sheet_id: str,
    gid: int,
    oauth_token_path: Path,
    week_end: dt.date | None = None,
) -> list[int]:
    """Read merchant IDs from the SDS MID tracker. Returns merchants that are:
      - in Column A (numeric MIDs)
      - NOT in EXCLUDED_MERCHANTS
      - have Date of Enabling ≤ week_end (if week_end given and date parses)

    Reads cols A (MID), B (Merchant Name), C (Date of Enabling). The date
    filter prevents merchants enabled AFTER the fetch window from being
    included — they'd have pre-enabling transactions in Trino but those
    don't count as SDS GMV.

    Auth via OAuth user (chargeback-automation@gokwik.co)."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(
        str(oauth_token_path),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            oauth_token_path.write_text(creds.to_json())
        else:
            raise SystemExit(
                f"OAuth token at {oauth_token_path} is invalid and cannot be "
                "refreshed. Re-run the OAuth flow that produced this token."
            )

    svc = build("sheets", "v4", credentials=creds)

    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tab = next(
        (s["properties"]["title"] for s in meta.get("sheets", [])
         if s["properties"]["sheetId"] == gid),
        None,
    )
    if tab is None:
        available = [(s["properties"]["title"], s["properties"]["sheetId"])
                     for s in meta.get("sheets", [])]
        raise SystemExit(f"No tab with gid={gid}. Available: {available}")

    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:C"
    ).execute()
    values = result.get("values", [])

    out: list[int] = []
    excluded_by_date: list[tuple[int, str, str]] = []
    for row in values[1:]:
        if not row:
            continue
        v = (row[0] or "").strip()
        if not v or v.lower() == "grand total":
            continue
        try:
            mid = int(float(v.replace(",", "")))
        except ValueError:
            continue
        if mid in EXCLUDED_MERCHANTS:
            continue
        # Date-of-enabling filter
        if week_end is not None and len(row) > 2:
            enable_date = _parse_enable_date(row[2] or "")
            if enable_date is not None and enable_date > week_end:
                excluded_by_date.append((mid, row[1] if len(row) > 1 else "", row[2]))
                continue
        out.append(mid)

    if excluded_by_date:
        print(f"  Excluded {len(excluded_by_date)} merchants enabled AFTER {week_end}:")
        for mid, name, date in excluded_by_date:
            print(f"    {mid:>8}  {name!r:<25}  enabled {date!r}")

    if len(out) < 50 or len(out) > 500:
        raise SystemExit(
            f"MID tracker returned {len(out)} merchant IDs — sanity-check "
            "failed (expected 50-500). Aborting to prevent silent drift."
        )
    return sorted(set(out))


def main() -> None:
    args = parse_args()
    start_d = dt.date.fromisoformat(args.start)
    end_d = dt.date.fromisoformat(args.end)

    parts_dir = args.parts_dir or Path(f".parts/production_{start_d}_{end_d}")
    parts_dir.mkdir(parents=True, exist_ok=True)

    bank_status = None if args.bank_status.lower() == "any" else args.bank_status

    # Pull merchant whitelist from the SDS MID tracker unless explicitly disabled.
    whitelist: list[int] | None = None
    if not args.no_sheet_filter:
        if not args.oauth_token.exists():
            raise SystemExit(
                f"OAuth token not found: {args.oauth_token}\n"
                "Either provide the OAuth token (from the chargeback-automation\n"
                "user) or pass --no-sheet-filter."
            )
        whitelist = get_merchant_whitelist(
            args.sheet_id, args.gid, args.oauth_token, week_end=end_d,
        )

    print(f"Window:             {start_d} → {end_d}")
    print(f"Date semantic:      unified_orders.created_at  (matches QS dashboard)")
    print(f"Exclusions:         {EXCLUDED_MERCHANTS}  (in MBQL filter)")
    print(f"payment_provider:   {args.payment_provider}")
    print(f"bank_status:        {bank_status or '(no filter)'}")
    if whitelist is not None:
        print(f"Merchant whitelist: {len(whitelist)} IDs from sheet "
              f"({args.sheet_id[:10]}…, gid={args.gid})")
    else:
        print(f"Merchant whitelist: NONE (--no-sheet-filter)")
    print(f"Parts dir:          {parts_dir}")
    print(f"Output:             {args.out}")
    print()

    tables = discover()

    t0 = time.time()
    days = list(iter_days(start_d, end_d))
    print(f"Top-level windows: {len(days)} day(s)")
    print()

    for i, (d_start, d_end) in enumerate(days, 1):
        print(f"[Day {i}/{len(days)}] {d_start.date()}", flush=True)
        fetch_window_adaptive(
            d_start, d_end, tables, parts_dir,
            args.min_minutes, args.attempts_per_level,
            args.payment_provider, bank_status,
            merchant_whitelist=whitelist,
        )

    parts = sorted(parts_dir.glob("*.csv"))
    print()
    print(f"Concatenating {len(parts)} part files → {args.out}")
    total = 0
    with open(args.out, "wb") as out:
        out.write(HEADER.encode())
        for p in parts:
            data = p.read_bytes()
            if not data:
                continue
            out.write(data)
            total += data.count(b"\n")
    elapsed = time.time() - t0
    print(f"Wrote {total:,} rows → {args.out}  (elapsed {elapsed/60:.1f} min)")


if __name__ == "__main__":
    main()
