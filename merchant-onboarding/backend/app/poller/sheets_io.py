"""Thin Google-Sheets fetchers for the two onboarding sheets.

Reuses the OAuth pattern from ops_infra/1/ (token.json already has
spreadsheets scope) so no second OAuth flow is needed. The shared
gmail_client.py was VENDORED into this package — we no longer poke
`sys.path` at import time to reach the chargeback codebase.

The fetchers return a list of dicts keyed by canonical column names
defined in COLUMN_MAP_*, so the rest of the poller never has to deal
with sheet ordering or whitespace.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build

from app.poller import gmail_client

HERE = Path(__file__).resolve().parent
# Where to find credentials.json + token.json. The default points at the
# chargeback codebase's OAuth directory (ops_infra/1/) because that's where
# the shared refresh token already lives; set GOOGLE_OAUTH_DIR to override
# (production deploys, alternate ops accounts, etc.).
_env_dir = os.environ.get("GOOGLE_OAUTH_DIR", "").strip()
ONE_DIR = Path(_env_dir) if _env_dir else HERE.parents[3] / "1"

# ---------------------------------------------------------------------------
# Sheet coordinates (from ops_infra/3/_sheet_inspection.json)
# ---------------------------------------------------------------------------
GOKWIK_SHEET_ID = "1-Mj_dTa1LTzyB2ucidNhDqQqNsS09rEC742t9t61cgk"
GOKWIK_TAB = "Merchant Onboarding"
# Pull cols A..K only. Col B (Signup/KYC Completion date By Gokwik) is read
# but dropped during parsing; everything past K (commercials / GMV / Key / Salt)
# is intentionally not requested.
GOKWIK_RANGE = f"'{GOKWIK_TAB}'!A1:K"

EASEBUZZ_SHEET_ID = "1X5e3r_0hz4oAf_6qu6mIrFEILco8pss-WmxlbVN_jio"
EASEBUZZ_TAB = "Easebuzz"
EASEBUZZ_RANGE = f"'{EASEBUZZ_TAB}'!A1:AM"


# ---------------------------------------------------------------------------
# Column maps: sheet header text  →  canonical key used in upserts.
# Stripped lowercase + whitespace-collapsed matching, so wording drift in the
# sheet (extra trailing space, casing) doesn't break the join.
# ---------------------------------------------------------------------------
def _h(s: str) -> str:
    """Header normalizer for fuzzy matching against the sheet's literal cells."""
    return " ".join((s or "").split()).strip().lower()


# Cols we want from the Gokwik Submerchant tab.
#
# Col B's header literally reads "Signup/KYC Completion date By Gokwik" but its
# actual contents for newer merchants (MID ≥ ~277000) are size classifications
# (`Emerging`, `Emerging - Custom`, `SME`) — the column was repurposed without
# renaming. Older rows have dates / "Not Embedded" in this cell instead.
# We import it as `merchant_size`; downstream code only uses the value when it
# looks like a real size string.
GOKWIK_COLUMN_MAP: dict[str, str] = {
    _h("MID"): "mid",                                       # A
    _h("Signup/KYC Completion date By Gokwik"): "merchant_size",  # B (repurposed)
    _h("EB Go LIVE Date -"): "eb_go_live_date",             # C
    _h("KYC SPOC"): "kyc_spoc",                             # D
    _h("Gokwik KYC complete date"): "gokwik_kyc_complete_date",  # E
    _h("Merchant Name"): "merchant_name",                   # F
    _h("Entity Name"): "entity_name",                       # G
    _h("Email ID"): "email",                                # H
    _h("Website"): "website",                               # I
    _h("Onboarding"): "onboarding",                         # J
    _h("Entity"): "entity",                                 # K
}

# Easebuzz tab. Note: the header has DUPLICATE "Time" and "Days Taken" cells —
# we resolve those by *position relative to neighbors*, not the header text alone.
# So this dict only handles unambiguous headers; the parser uses positional
# fall-backs for the duplicated ones.
EASEBUZZ_COLUMN_MAP: dict[str, str] = {
    _h("Merchant Name"): "merchant_name",
    _h("Merchant size"): "merchant_size",
    _h("Onboarding Status"): "onboarding_status",
    _h("Kickstart Date"): "kickstart_date",
    _h("Docs Received Date"): "docs_received_date",
    _h("Days Taken\n(KS - DS)"): "days_taken_ks_to_ds",
    _h("Days Taken (KS - DS)"): "days_taken_ks_to_ds",
    _h("Time Taken"): "time_taken_ks_to_ds",
    _h("KYC Completed by Ops"): "kyc_completed_by_ops",
    _h("Date of Email sent to EB"): "date_email_sent_to_eb",
    _h("Salt and Key Receipt"): "salt_key_receipt",
    _h("Time Taken by EB"): "time_taken_by_eb",
    _h("Salt key from Docs recd"): "salt_key_from_docs_recd",
    _h("Salt key from Kickstart"): "salt_key_from_kickstart",
    _h("Reasons for Delay in EB Submission"): "reasons_for_delay_in_eb",
    _h("Promise"): "promise",
    _h("Delivery"): "delivery",
    _h("Remarks"): "remarks",
    _h("Delay at GK"): "delay_at_gk",
    _h("Delay by Merchant"): "delay_by_merchant",
    _h("Ops Remarks"): "ops_remarks",
}


# ---------------------------------------------------------------------------
# Auth + raw fetch
# ---------------------------------------------------------------------------
def _creds_paths() -> tuple[str, str]:
    creds = os.environ.get("GOOGLE_CREDENTIALS_JSON") or str(ONE_DIR / "credentials.json")
    token = os.environ.get("GOOGLE_TOKEN_JSON") or str(ONE_DIR / "token.json")
    return creds, token


def build_sheets_service() -> Any:
    creds_path, token_path = _creds_paths()
    creds = gmail_client.build_oauth_creds(creds_path, token_path)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _fetch_rows(svc, spreadsheet_id: str, range_a1: str) -> list[list[str]]:
    return (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=range_a1)
        .execute()
        .get("values", [])
    )


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------
def _index_header(
    header: list[str], primary_map: dict[str, str],
    secondary_map: dict[str, str] | None = None,
) -> tuple[dict[str, int], dict[str, int], list[tuple[int, str]]]:
    """Return ({canonical -> col_idx}, {commercial_key -> col_idx}, [(col_idx, raw_header)] for unmapped)."""
    primary: dict[str, int] = {}
    secondary: dict[str, int] = {}
    unmapped: list[tuple[int, str]] = []
    for i, raw in enumerate(header):
        key = _h(raw)
        if key in primary_map and primary_map[key] not in primary:
            primary[primary_map[key]] = i
        elif secondary_map and key in secondary_map and secondary_map[key] not in secondary:
            secondary[secondary_map[key]] = i
        else:
            unmapped.append((i, raw))
    return primary, secondary, unmapped


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def fetch_gokwik_merchants(svc) -> list[dict[str, Any]]:
    """Pull cols A and C..K from the Gokwik Submerchant list. Col B is dropped.

    Returns one dict per row that has a non-empty MID.

    Raises:
        ValueError: when the sheet's header is missing `mid` or `merchant_name`
        — without those two columns there's nothing to upsert and continuing
        would silently nuke the merchants table on conflict.
    """
    rows = _fetch_rows(svc, GOKWIK_SHEET_ID, GOKWIK_RANGE)
    if not rows:
        return []
    header = rows[0]
    primary, _, _ = _index_header(header, GOKWIK_COLUMN_MAP)
    if "mid" not in primary or "merchant_name" not in primary:
        raise ValueError(
            f"Gokwik sheet missing required column. Found headers: {header!r}"
        )
    out: list[dict[str, Any]] = []
    for row in rows[1:]:
        mid = _cell(row, primary.get("mid"))
        if not mid:
            continue
        rec: dict[str, Any] = {"mid": mid}
        for key, idx in primary.items():
            if key == "mid":
                continue
            rec[key] = _cell(row, idx)
        out.append(rec)
    return out


def fetch_easebuzz_onboarding(svc) -> list[dict[str, Any]]:
    """Pull the Easebuzz tab. Returns one dict per row with a Merchant Name.

    Raises:
        ValueError: when the sheet's header is missing `merchant_name`
        — without it we'd insert rows with name_normalized='' which would
        all collide on the UNIQUE constraint and report bogus stats.
    """
    rows = _fetch_rows(svc, EASEBUZZ_SHEET_ID, EASEBUZZ_RANGE)
    if not rows:
        return []
    header = rows[0]
    primary, _, _unmapped = _index_header(header, EASEBUZZ_COLUMN_MAP)
    if "merchant_name" not in primary:
        raise ValueError(
            f"Easebuzz sheet missing required column. Found headers: {header!r}"
        )

    # The Easebuzz header has two un-labelled "Time" columns sitting immediately
    # to the right of "Kickstart Date" and "Docs Received Date" respectively.
    # Resolve them positionally so we capture both.
    kickstart_time_idx: int | None = None
    docs_time_idx: int | None = None
    ks_idx = primary.get("kickstart_date")
    ds_idx = primary.get("docs_received_date")
    if ks_idx is not None and ks_idx + 1 < len(header) and _h(header[ks_idx + 1]) == "time":
        kickstart_time_idx = ks_idx + 1
    if ds_idx is not None and ds_idx + 1 < len(header) and _h(header[ds_idx + 1]) == "time":
        docs_time_idx = ds_idx + 1

    # A "Days Taken" appears twice as well (after KS-DS and after KYC). The
    # second is what we want for `days_taken_kyc`.
    days_taken_kyc_idx: int | None = None
    kyc_idx = primary.get("kyc_completed_by_ops")
    if kyc_idx is not None and kyc_idx + 1 < len(header) and _h(header[kyc_idx + 1]).startswith("days taken"):
        days_taken_kyc_idx = kyc_idx + 1

    out: list[dict[str, Any]] = []
    for row in rows[1:]:
        name = _cell(row, primary.get("merchant_name"))
        if not name:
            continue
        rec: dict[str, Any] = {"merchant_name": name}
        for key, idx in primary.items():
            if key == "merchant_name":
                continue
            rec[key] = _cell(row, idx)
        rec["kickstart_time"] = _cell(row, kickstart_time_idx)
        rec["docs_received_time"] = _cell(row, docs_time_idx)
        rec["days_taken_kyc"] = _cell(row, days_taken_kyc_idx)
        out.append(rec)
    return out
