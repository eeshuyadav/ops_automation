"""Planhat client — looks up merchant -> CSM emails.

Strategy: bulk-load all companies + users on first call (per --once run) and
cache in process. Subsequent merchant lookups are pure dict accesses.

Cost per run: ~3 API calls + ~2-5 seconds, regardless of how many merchants
we look up. Better than per-merchant queries which hit a default 100-record
page that doesn't actually filter.

Public:
    PlanhatClient(token_path, base_url)
    .get_csm_emails(merchant_name) -> list[str]

`get_csm_emails` returns BOTH the company's owner AND coOwner emails when
present, deduped — production decision is to Cc both.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _normalize(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"[\s._\-'\"/&]+", "", s).lower()


class PlanhatClient:
    def __init__(self, token_path: str, base_url: str = "https://api.planhat.com"):
        p = Path(token_path)
        if not p.exists():
            raise FileNotFoundError(f"planhat token not found: {p}")
        token = p.read_text().strip()
        # Tolerate the "API token=..." prefix in case someone forgets to strip.
        if token.startswith("API token="):
            token = token.split("=", 1)[1].strip()
        self._auth = {"Authorization": f"Bearer {token}"}
        self.base_url = base_url.rstrip("/")
        self._companies_by_norm_name: dict[str, dict] | None = None
        self._companies_by_domain: dict[str, dict] = {}
        self._user_email_by_id: dict[str, str] | None = None

    # --- HTTP --------------------------------------------------------------

    def _get(self, path: str, timeout: int = 30):
        req = urllib.request.Request(f"{self.base_url}{path}", headers=self._auth)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    # --- bulk loaders (run once per --once invocation, cached) -------------

    def _ensure_loaded(self) -> None:
        if self._companies_by_norm_name is not None:
            return

        # Pull all companies (paginated 5000/page).
        all_co: list[dict] = []
        offset = 0
        while True:
            page = self._get(
                f"/companies?limit=5000&offset={offset}"
                "&select=name,coOwner,owner,domains,externalId"
            )
            if not page:
                break
            all_co.extend(page)
            if len(page) < 5000:
                break
            offset += 5000

        # Build name index. If duplicates, the last write wins — they're rare
        # and Planhat normally enforces unique names.
        idx: dict[str, dict] = {}
        by_domain: dict[str, dict] = {}
        for c in all_co:
            key = _normalize(c.get("name") or "")
            if key:
                idx[key] = c
            for d in c.get("domains") or []:
                d_lower = (d or "").strip().lower()
                if d_lower and d_lower not in by_domain:
                    by_domain[d_lower] = c
        self._companies_by_norm_name = idx
        self._companies_by_domain = by_domain

        # Pull users (small — ~150 entries — single page).
        users = self._get("/users?limit=2000&select=_id,email")
        self._user_email_by_id = {
            u["_id"]: u.get("email", "")
            for u in users if isinstance(u, dict) and u.get("_id")
        }

    # --- public API --------------------------------------------------------

    def _find_company(self, merchant_name: str | None,
                      orig_to_email: str | None = None) -> dict | None:
        """Multi-tier match against Planhat companies. Order:
            1. exact normalized-name
            2. domain match against orig_to_email's domain
            3. bidirectional prefix-name match (min 5 chars)
        """
        self._ensure_loaded()
        assert self._companies_by_norm_name is not None
        idx = self._companies_by_norm_name

        # 1) exact
        key = _normalize(merchant_name or "")
        if key and key in idx:
            return idx[key]

        # 2) domain
        if orig_to_email and "@" in orig_to_email:
            dom = orig_to_email.split("@", 1)[1].strip().lower()
            if dom in self._companies_by_domain:
                return self._companies_by_domain[dom]

        # 3) bidirectional prefix (min length 5 on the shorter side)
        if key and len(key) >= 5:
            best: tuple[int, dict] | None = None
            for sk, company in idx.items():
                short = sk if len(sk) < len(key) else key
                if len(short) < 5:
                    continue
                if sk.startswith(key) or key.startswith(sk):
                    if best is None or len(short) > best[0]:
                        best = (len(short), company)
            if best is not None:
                return best[1]
        return None

    def get_csm_emails(self, merchant_name: str | None,
                       orig_to_email: str | None = None) -> list[str]:
        """Return [owner_email, coOwner_email] (deduped, blank-stripped) for
        the matched company. Empty list if no match."""
        self._ensure_loaded()
        assert self._user_email_by_id is not None
        company = self._find_company(merchant_name, orig_to_email)
        if not company:
            return []
        out: list[str] = []
        for field in ("owner", "coOwner"):
            v = company.get(field)
            user_id = v.get("_id") if isinstance(v, dict) else v
            if user_id:
                email = self._user_email_by_id.get(user_id, "")
                if email and email not in out:
                    out.append(email)
        return out

    def get_company(self, merchant_name: str | None) -> dict | None:
        """Return the raw company record (for diagnostics)."""
        if not merchant_name:
            return None
        self._ensure_loaded()
        assert self._companies_by_norm_name is not None
        return self._companies_by_norm_name.get(_normalize(merchant_name))
