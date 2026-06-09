"""Extract fields from an HTML email body.

The Easebuzz chargeback mail renders two shapes of the same data:
  1. A wide header/data table: <tr>header cells</tr><tr>value cells</tr>
  2. A label:value stack: <tr><td>Case ID</td><td>181398</td></tr> ...

This module tries both and returns a merged dict keyed by our internal names.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _parse_label_value_rows(soup: BeautifulSoup) -> dict[str, str]:
    """Find <tr> with exactly two cells and treat them as (label, value)."""
    out: dict[str, str] = {}
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) != 2:
            continue
        label = _clean(cells[0].get_text())
        value = _clean(cells[1].get_text())
        if label and value and label != value:
            out.setdefault(label, value)
    return out


def _parse_header_data_tables(soup: BeautifulSoup) -> dict[str, str]:
    """Find tables whose first row is all <th> and second row the values."""
    out: dict[str, str] = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        head_cells = rows[0].find_all(["th", "td"])
        # Treat as header row only if every cell is <th> or bolded.
        is_header = rows[0].find_all("th") and len(rows[0].find_all("th")) == len(head_cells)
        if not is_header:
            continue
        data_cells = rows[1].find_all(["td", "th"])
        if len(data_cells) != len(head_cells):
            continue
        for h, d in zip(head_cells, data_cells):
            label = _clean(h.get_text())
            value = _clean(d.get_text())
            if label and value:
                out.setdefault(label, value)
    return out


_SALUTATION_RE = re.compile(
    r"\bDear\s+([A-Za-z][A-Za-z0-9 .&'\-]{0,60}?)\s*[,!\n]",
    re.IGNORECASE,
)

# Generic greetings that aren't a real merchant name. If we find one of these
# first (e.g. "Dear Team," at the top of a forwarded mail), we keep searching.
_GENERIC_GREETINGS = {"sir", "madam", "team", "merchant", "customer", "all", "partner"}


_BLOCKQUOTE_RE = re.compile(r"<blockquote", re.IGNORECASE)
_WROTE_RE      = re.compile(r"\bwrote\s*:", re.IGNORECASE)
_FORWARDED_RE  = re.compile(r"-{3,}\s*Forwarded\s+message\s*-{3,}", re.IGNORECASE)


def has_quoted_reply(html: str) -> bool:
    """Return True if the body contains a quoted chain of prior replies.

    A fresh chargeback notification arrives via the Google Group and already
    contains one blockquote / one "wrote:" from the DL wrapping — that's
    baseline, not a conversation. Count-based thresholds distinguish
    "conversation in progress" from "standalone fresh chargeback".
    """
    if not html:
        return False
    if _FORWARDED_RE.search(html):
        return True
    if len(_BLOCKQUOTE_RE.findall(html)) > 1:
        return True
    if len(_WROTE_RE.findall(html)) > 1:
        return True
    return False


def extract_merchant_name(html: str) -> str | None:
    """Pull the merchant name from a 'Dear <name>,' salutation.

    Forwarded mails often have a generic salutation ("Dear Team,") at the top
    followed by the original Easebuzz salutation ("Dear OmaLiving,") deeper
    down. We iterate all matches and return the first one that isn't generic.
    """
    text = BeautifulSoup(html or "", "lxml").get_text("\n")
    for m in _SALUTATION_RE.finditer(text):
        name = _clean(m.group(1))
        if not name or name.lower() in _GENERIC_GREETINGS:
            continue
        return name
    return None


def parse_body_fields(html: str, labels: dict[str, str]) -> dict[str, str | None]:
    """Given the mail body HTML and a mapping {field_name -> label_in_table},
    return {field_name -> value or None}.

    Also populates `merchant_name` from the 'Dear X,' salutation when present.
    """
    soup = BeautifulSoup(html or "", "lxml")
    found: dict[str, str] = {}
    found.update(_parse_header_data_tables(soup))
    for k, v in _parse_label_value_rows(soup).items():
        found.setdefault(k, v)

    # Case-insensitive lookup for labeled fields.
    lower = {k.lower(): v for k, v in found.items()}
    out: dict[str, str | None] = {}
    for field, label in labels.items():
        out[field] = lower.get(label.lower())

    # Salutation-derived merchant name.
    if "merchant_name" in labels:
        out["merchant_name"] = extract_merchant_name(html) or out.get("merchant_name")
    return out
