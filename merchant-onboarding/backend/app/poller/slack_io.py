"""Slack reader for the Easebuzz salt & key channel.

The channel `C0ALVC72T6K` hosts a daily digest posted by an internal bot,
shaped like:

    :e-mail: *Easebuzz Key & Salt — April 30, 2026*
    Total: *15 merchant(s)*

    1. Born16 | MID:273430 | Key:U14ZD4XCQC | Salt:ELNBG603UE | Email:... | CC EMI:Enabled
    2. Glimmora by groverlights | MID:275396 | Key:... | Salt:... | ...
    ...

We pull each digest, parse the header date (= the date Easebuzz issued the
batch), and yield (mid, salt_key_date, permalink) tuples. Caller writes the
date into `easebuzz_onboarding.salt_key_receipt` for the matching MID.

Hard rules:
  * NEVER store the Key or Salt values. The dashboard only cares about the date.
  * NEVER log message text — it contains live credentials. We log only counts
    and the merchant IDs we matched.

`fetch_salt_key_records()` is the only public surface. It's a generator so the
poller can stream-update one record at a time without holding everything in
memory.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterator

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


logger = logging.getLogger(__name__)


# Header carries `&amp;` because Slack HTML-escapes ampersands in message text.
# Date in the header is the salt&key issuance date (e.g. "April 30, 2026").
_HEADER_RE = re.compile(
    r"\*Easebuzz Key &amp; Salt\s+[—-]\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})\*"
)
# Numbered merchant lines: "12. Some Name | MID:273430 | Key:... | Salt:... | ..."
_MID_RE = re.compile(r"\bMID:\s*(\d+)\b")


@dataclass(frozen=True)
class SaltKeyRecord:
    mid: str
    salt_key_date: date          # parsed from the header, NOT the message ts
    salt_key_date_text: str      # ISO date string for the TEXT column
    permalink: str | None        # for ops to click through if they want
    posted_at: datetime          # message timestamp (UTC)


def build_slack_client(token: str) -> WebClient | None:
    """Return a configured WebClient, or None if the token is blank.

    Caller is expected to short-circuit Slack sync when this returns None.
    """
    token = (token or "").strip()
    if not token:
        return None
    return WebClient(token=token)


def _parse_header_date(raw: str) -> date | None:
    """Parse 'April 30, 2026' / 'May 02, 2026' / 'May 2, 2026' → date.

    Used for the salt&key receipt date. Returns None on any parse failure —
    caller skips the message rather than aborting the whole sync.
    """
    raw = raw.strip().replace(",", "")
    # Strict format gauntlet — no fuzzy parsing.
    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _iter_history(
    client: WebClient, channel_id: str, oldest_ts: float,
) -> Iterator[dict]:
    """Stream messages from conversations.history, oldest filter applied.

    Pagination uses Slack's cursor scheme. We yield raw message dicts to
    keep this module testable without a live API.
    """
    cursor: str | None = None
    while True:
        kwargs: dict = {
            "channel": channel_id,
            "oldest": str(oldest_ts),
            "limit": 200,
        }
        if cursor:
            kwargs["cursor"] = cursor
        resp = client.conversations_history(**kwargs)
        for msg in resp.get("messages", []):
            yield msg
        if not resp.get("has_more"):
            break
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break


def fetch_salt_key_records(
    client: WebClient,
    channel_id: str,
    *,
    since: datetime,
    fetch_permalinks: bool = True,
) -> Iterator[SaltKeyRecord]:
    """Yield (mid, header_date) pairs from every batch digest since `since`.

    Skips:
      - "No Easebuzz Key & Salt email found for X" notices
      - channel-join system messages
      - any malformed batch (header date won't parse)

    Does NOT yield duplicates if the same MID appears in multiple batches —
    the caller is responsible for the upsert logic (and typically wants the
    most recent date anyway, which falls out naturally from `oldest-first`
    iteration + idempotent UPDATE).
    """
    oldest_ts = since.timestamp()

    matched = 0
    batches = 0
    skipped_no_email = 0
    skipped_other = 0

    messages = list(_iter_history(client, channel_id, oldest_ts))
    # Sort oldest-first so newer digests overwrite older ones for the same MID
    # in the caller's UPDATE loop — usually a no-op, but defensive.
    messages.sort(key=lambda m: float(m.get("ts", "0")))

    for msg in messages:
        text = msg.get("text") or ""
        header = _HEADER_RE.search(text)
        if not header:
            if "No Easebuzz Key &amp; Salt email" in text:
                skipped_no_email += 1
            else:
                skipped_other += 1
            continue

        sk_date = _parse_header_date(header.group(1))
        if sk_date is None:
            logger.warning("slack: unparseable header date %r — skipping batch", header.group(1))
            continue

        batches += 1
        mids = _MID_RE.findall(text)
        if not mids:
            logger.warning("slack: batch with no MIDs (date=%s) — skipping", sk_date.isoformat())
            continue

        permalink: str | None = None
        if fetch_permalinks:
            try:
                pr = client.chat_getPermalink(channel=channel_id, message_ts=msg["ts"])
                permalink = pr.get("permalink")
            except SlackApiError as e:
                logger.warning("slack: permalink fetch failed (%s)", e.response.get("error"))

        posted = datetime.fromtimestamp(float(msg.get("ts", "0")), tz=timezone.utc)

        # Match the sheet's date format ("dd-MMM-yy", e.g. 21-May-26) so the
        # Salt&Key column reads consistently with Kickstart / Docs Recd /
        # Email-to-EB on the dashboard. `%b` is the locale's abbreviated
        # month name — on a POSIX server this is always English (Jan, Feb, …).
        sk_text = sk_date.strftime("%d-%b-%y")

        for mid in mids:
            matched += 1
            yield SaltKeyRecord(
                mid=mid,
                salt_key_date=sk_date,
                salt_key_date_text=sk_text,
                permalink=permalink,
                posted_at=posted,
            )

    logger.info(
        "slack: scanned %d messages — batches=%d, mid_records=%d, "
        "no_email=%d, other_skipped=%d",
        len(messages), batches, matched, skipped_no_email, skipped_other,
    )
