"""Merchant-name normalization shared by the poller and any join logic.

Lifted from ops_infra/1/sheets_client.py::_normalize so both codebases match.
"""
from __future__ import annotations

import re

_STRIP = re.compile(r"[\s._\-'\"/&]+")


def normalize_name(s: str | None) -> str:
    if not s:
        return ""
    return _STRIP.sub("", s).lower()
