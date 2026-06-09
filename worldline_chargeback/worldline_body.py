"""Extract fields from a Worldline "New Chargeback Request" mail body.

Worldline emails carry a single HTML table with two rows:
    row 0  =  column headers (Bank_Ticket, Bank_RRN, TPSL_ID, Transaction_Date,
              TPSL_Txn_Amount, Bank_Disputed_Amount, SM_Transaction_ID_SRC_PRN,
              Status, Src_Code_Live_ID, Merchant_Name, PG_ID_RET_BID,
              Adjustment_Type, Reason_Code, Chargeback_Reason, SRC_ITC)
    row 1  =  corresponding values

Both rows are <td> (not <th>), so the existing /1/body_parser cannot pick it
up. This module zips the two rows into a dict and returns the canonical
keys we use downstream.
"""
from __future__ import annotations

import re
from typing import Iterable

from bs4 import BeautifulSoup


# Headers we recognize in the first row of the Worldline table. If at least
# `_MIN_HEADER_HITS` of these appear, we treat the table as the chargeback
# data table.
_KNOWN_HEADERS: tuple[str, ...] = (
    "Bank_Ticket", "Bank_RRN", "TPSL_ID", "Transaction_Date",
    "TPSL_Txn_Amount", "Bank_Disputed_Amount",
    "SM_Transaction_ID_SRC_PRN", "Status", "Src_Code_Live_ID",
    "Merchant_Name", "PG_ID_RET_BID", "Adjustment_Type",
    "Reason_Code", "Chargeback_Reason", "SRC_ITC",
)
_MIN_HEADER_HITS = 4


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _text(cell) -> str:
    return _norm(cell.get_text(separator=" "))


def parse_fields(html: str) -> dict[str, str]:
    """Return canonical field dict extracted from the Worldline body. Keys
    include payment_id, merchant_name, bank_ticket, bank_rrn, tpsl_id,
    transaction_date, tpsl_txn_amount, bank_disputed_amount, reason_code,
    chargeback_reason. Empty values are dropped.
    """
    if not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        head_cells = rows[0].find_all(["td", "th"])
        data_cells = rows[1].find_all(["td", "th"])
        if not head_cells or len(head_cells) != len(data_cells):
            continue
        # Are enough known headers present?
        hits = sum(
            1 for h in head_cells
            if _text(h) in _KNOWN_HEADERS
        )
        if hits < _MIN_HEADER_HITS:
            continue
        raw: dict[str, str] = {}
        for h, d in zip(head_cells, data_cells):
            label = _text(h)
            value = _text(d)
            if label:
                raw[label] = value
        # Map to canonical keys.
        return {
            "payment_id":             raw.get("SM_Transaction_ID_SRC_PRN", ""),
            "merchant_name_body":     raw.get("Merchant_Name", ""),
            "bank_ticket":            raw.get("Bank_Ticket", ""),
            "bank_rrn":               raw.get("Bank_RRN", ""),
            "tpsl_id":                raw.get("TPSL_ID", ""),
            "transaction_date":       raw.get("Transaction_Date", ""),
            "tpsl_txn_amount":        raw.get("TPSL_Txn_Amount", ""),
            "bank_disputed_amount":   raw.get("Bank_Disputed_Amount", ""),
            "status":                 raw.get("Status", ""),
            "src_code_live_id":       raw.get("Src_Code_Live_ID", ""),
            "pg_id_ret_bid":          raw.get("PG_ID_RET_BID", ""),
            "adjustment_type":        raw.get("Adjustment_Type", ""),
            "reason_code":            raw.get("Reason_Code", ""),
            "chargeback_reason":      raw.get("Chargeback_Reason", ""),
            "src_itc":                raw.get("SRC_ITC", ""),
        }
    return {}


# ---------------------------------------------------------------------------
# Subject parsing — captures the merchant name and case/RRN id between
# "For" and the separator. Supports both "= " and "===" separators and an
# optional "(URGENT)" suffix.
#
# Examples:
#   "Worldline New Chargeback Request For Mivi ===UP1303705"
#   "Worldline New Chargeback Request For NEXXBASE MARKETING PRIVATE LIMITED = 190966035362"
#   "Worldline New Chargeback Request For Boat = UP1304600 (URGENT)"
# ---------------------------------------------------------------------------
_SUBJECT_RE = re.compile(
    r"Worldline\s+New\s+Chargeback\s+Request\s+For\s+(?P<merchant>.+?)\s*=+\s*(?P<case>\S+)",
    re.IGNORECASE,
)


def parse_subject(subject: str) -> dict[str, str]:
    """Return {"merchant_name", "case_id"} parsed from a Worldline subject
    line, or {} if the line does not match."""
    if not subject:
        return {}
    # Strip optional "Re: " / "Fwd: " prefixes.
    s = re.sub(r"^\s*(re|fwd|fw)\s*:\s*", "", subject, flags=re.IGNORECASE)
    m = _SUBJECT_RE.search(s)
    if not m:
        return {}
    merchant = _norm(m.group("merchant"))
    case = m.group("case").rstrip(".,;:")
    # Strip trailing "(URGENT)" markers from the case token if they leak in.
    case = re.sub(r"\s*\(.*\)\s*$", "", case)
    return {"merchant_name": merchant, "case_id": case}
