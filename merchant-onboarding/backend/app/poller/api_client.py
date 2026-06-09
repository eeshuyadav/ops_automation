"""Kickoff API client.

GoKwik exposes a "merchant-kickoff-data" endpoint that returns the official
Kickstart Date (and Live Date) per merchant. We hit it once per sync with the
date range that covers all merchants being seeded — the API docs warn against
frequent polling and prefer small ranges.

Endpoint:
    GET {KICKOFF_API_BASE}/api/merchant-kickoff-data?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD

Response:
    {
        "success": true,
        "total": 10,
        "data": [
            {"merchantname": "...", "kickoff": "2026-05-15", "livedate": "2026-05-16"},
            ...
        ]
    }

`kickoff` may be either "YYYY-MM-DD" or "YYYY-MM-DDTHH:mm" — we strip any time
part to keep the date column clean.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from typing import TypedDict
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from app.poller.normalize import normalize_name


DEFAULT_BASE = "https://dev-mintdash.dev.gokwik.io"
DEFAULT_TIMEOUT = 30  # seconds


class KickoffInfo(TypedDict, total=False):
    kickoff: str         # "YYYY-MM-DD" (time stripped if API returned ISO datetime)
    livedate: str        # "YYYY-MM-DD" or "" when not yet live
    merchantname: str    # raw name as returned by the API


def _base_url() -> str:
    return os.environ.get("KICKOFF_API_BASE", DEFAULT_BASE).rstrip("/")


def _strip_time(s: str | None) -> str:
    """API may return either YYYY-MM-DD or YYYY-MM-DDTHH:mm — keep just the date."""
    if not s:
        return ""
    return s.split("T", 1)[0].strip()


def fetch_kickoff_data(start_date: date, end_date: date) -> dict[str, KickoffInfo]:
    """Hit the API for [start_date, end_date] and return a lookup by normalized name.

    Returns {} if the API request fails — callers should treat missing kickoff
    data as "not available, leave blank" rather than aborting the whole sync.
    Raises only on truly malformed responses (so a programming bug surfaces).
    """
    if start_date > end_date:
        raise ValueError(f"start_date {start_date} > end_date {end_date}")

    qs = urlencode({"startDate": start_date.isoformat(), "endDate": end_date.isoformat()})
    url = f"{_base_url()}/api/merchant-kickoff-data?{qs}"

    req = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
    except HTTPError as e:
        print(f"WARN: kickoff API HTTP {e.code} for {start_date}..{end_date}: {e.reason}",
              file=sys.stderr)
        return {}
    except URLError as e:
        print(f"WARN: kickoff API unreachable for {start_date}..{end_date}: {e.reason}",
              file=sys.stderr)
        return {}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        print(f"WARN: kickoff API returned non-JSON: {body[:200]!r}", file=sys.stderr)
        return {}

    if not payload.get("success"):
        print(f"WARN: kickoff API not-success: {payload.get('message')!r}",
              file=sys.stderr)
        return {}

    out: dict[str, KickoffInfo] = {}
    for item in payload.get("data", []) or []:
        name = (item.get("merchantname") or "").strip()
        if not name:
            continue
        key = normalize_name(name)
        if not key:
            continue
        # If two rows hit the same normalized key, the later wins — both rows
        # presumably describe the same merchant with whitespace/casing drift.
        out[key] = {
            "merchantname": name,
            "kickoff": _strip_time(item.get("kickoff")),
            "livedate": _strip_time(item.get("livedate")),
        }
    return out
