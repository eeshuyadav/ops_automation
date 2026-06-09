"""Resolve a merchant from the Merchant Onboarding Google Sheet, given the
website URL extracted from a PayU chargeback notification body.

The Onboarding sheet has rows like:
    | Entity Name | Website            | Email ID            | ... |

We need a lookup keyed by URL that tolerates:
    - https:// vs http:// (try both)
    - trailing slashes
    - www. prefix differences
    - case-insensitivity

If a match is found, return both the merchant's Email ID and Entity Name.
Entity Name is then used to find the CSM in Planhat.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import sheets_client as sc


def _normalize_url(raw: str) -> str:
    """Strip scheme + trailing slash + www. + lowercase. Used as the lookup key.

    Examples
    --------
    >>> _normalize_url("https://www.MyFrido.com/")
    'myfrido.com'
    >>> _normalize_url("http://example.com")
    'example.com'
    >>> _normalize_url("example.com/")
    'example.com'
    """
    if not raw:
        return ""
    s = raw.strip().lower()
    # Strip scheme if present
    if "://" in s:
        s = urlparse(s).netloc or s.split("://", 1)[1]
    # Strip trailing path/slashes
    s = s.split("/", 1)[0]
    # Strip www.
    if s.startswith("www."):
        s = s[4:]
    return s.rstrip(".")


def build_lookup(
    creds,
    cfg_merchant: dict,
) -> dict[str, dict]:
    """Read the Onboarding sheet and return a dict keyed by normalized URL:
        {"myfrido.com": {"email": "...", "entity": "MyFrido"}, ...}

    Header column names come from cfg_merchant (url_col / email_col /
    entity_col); matching is case- and whitespace-insensitive against the
    actual header row in the sheet.
    """
    spreadsheet_id = cfg_merchant["spreadsheet_id"]
    rng_a1 = cfg_merchant.get("range", "A1:Z")

    # If a specific tab is needed, the caller should pass `range` already
    # qualified (e.g. "'Master'!A1:Z"). Otherwise we read the first sheet.
    rows = sc.fetch_rows(creds, spreadsheet_id, rng_a1)
    if not rows:
        return {}

    header = rows[0]
    want_url    = _norm(cfg_merchant["url_col"])
    want_email  = _norm(cfg_merchant["email_col"])
    want_entity = _norm(cfg_merchant["entity_col"])
    want_brand  = _norm(cfg_merchant.get("brand_col", "Merchant Name"))

    def _find(target: str, required: bool = True) -> int:
        for i, h in enumerate(header):
            if _norm(h) == target:
                return i
        if required:
            raise KeyError(f"column {target!r} not found in header: {header}")
        return -1

    i_url    = _find(want_url)
    i_email  = _find(want_email)
    i_entity = _find(want_entity)
    i_brand  = _find(want_brand, required=False)  # optional — fallback to entity

    out: dict[str, dict] = {}
    for row in rows[1:]:
        if not row:
            continue
        raw_url = row[i_url] if i_url < len(row) else ""
        key = _normalize_url(raw_url)
        if not key:
            continue
        email  = row[i_email]  if i_email  < len(row) else ""
        entity = row[i_entity] if i_entity < len(row) else ""
        brand  = (row[i_brand] if (i_brand >= 0 and i_brand < len(row)) else "")
        out.setdefault(key, {
            "email":   (email or "").strip(),
            "entity":  (entity or "").strip(),
            "brand":   (brand or "").strip(),
            "raw_url": (raw_url or "").strip(),
        })
    return out


def _norm(s: str) -> str:
    """Header-key normalization: collapse whitespace, strip, lowercase."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", s.strip()).lower()


def resolve(url: str, lookup: dict[str, dict]) -> dict:
    """Find the merchant block for `url`. Returns {} if no row matches.

    Strict match — only these variants are accepted:
        1. Exact normalized match (https/http/trailing-slash agnostic).
        2. www. ↔ no-www swap.

    Anything else (e.g. apex `myfrido.com` against subdomain
    `mobility.myfrido.com`) is treated as NOT a match — the thread falls
    through to the manual-review tracker, deliberately, so the team can
    add the missing Website value in the Onboarding sheet rather than the
    bot guessing.
    """
    base = _normalize_url(url)
    if not base:
        return {}
    if base in lookup:
        return lookup[base]
    alt = base[4:] if base.startswith("www.") else f"www.{base}"
    return lookup.get(alt, {})
