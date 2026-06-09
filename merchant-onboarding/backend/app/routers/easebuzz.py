from __future__ import annotations

import uuid
from datetime import datetime, timezone

import statistics
from collections import Counter
from datetime import date, timedelta

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, case, cast, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.types import Integer

from app import models, schemas
from app.adapters import easebuzz_out
from app.db import get_db


SPEED_BUCKET_ORDER = ["0-1d", "2-3d", "4-7d", "8-14d", "15+d", "unknown"]


def _to_int_or_none(v: str | None) -> int | None:
    """Parse a TEXT day-count to a non-negative int. Tolerates whitespace.

    Negative numbers can't happen in steady state (the poller forces
    salt_key_from_kickstart positive; the other two should be too), but
    we abs() defensively just in case a legacy row still has one.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return abs(int(s))
    except ValueError:
        return None


def _bucket(n: int | None) -> str:
    if n is None:
        return "unknown"
    if n <= 1:  return "0-1d"
    if n <= 3:  return "2-3d"
    if n <= 7:  return "4-7d"
    if n <= 14: return "8-14d"
    return "15+d"


def _speed_metric(values: list[str | None]) -> schemas.SpeedMetric:
    """Bucket + summarize a column of duration strings.

    Computes min / max / mean / p25 / median / p75 / p90 plus a 5-bucket
    distribution. Percentiles use `statistics.quantiles` which silently
    requires at least N data points for the N-th percentile to be well
    defined; below that threshold we leave the field as None (the UI
    renders "—") rather than emit a misleading number from too few rows.
    """
    parsed = [_to_int_or_none(v) for v in values]
    numeric = [n for n in parsed if n is not None]

    counts: Counter[str] = Counter()
    for n in parsed:
        counts[_bucket(n)] += 1

    buckets = [
        schemas.SpeedBucket(bucket=b, count=counts.get(b, 0))
        for b in SPEED_BUCKET_ORDER
    ]

    if not numeric:
        return schemas.SpeedMetric(total=0, buckets=buckets)

    median = statistics.median(numeric)
    mean   = statistics.fmean(numeric)
    mn     = min(numeric)
    mx     = max(numeric)
    # `statistics.quantiles` needs n+1 distinct samples; below that we keep
    # the per-percentile field None instead of fabricating a value.
    p25 = p75 = p90 = None
    if len(numeric) >= 4:
        qs = statistics.quantiles(numeric, n=4)
        p25 = qs[0]
        p75 = qs[2]
    if len(numeric) >= 10:
        qs10 = statistics.quantiles(numeric, n=10)
        p90 = qs10[8]
    return schemas.SpeedMetric(
        total=len(numeric),
        min=mn,
        max=mx,
        mean=mean,
        p25=p25,
        median=median,
        p75=p75,
        p90=p90,
        buckets=buckets,
    )

router = APIRouter(prefix="/api/easebuzz", tags=["easebuzz"])


def _resolve_window(
    days: int | None, start_date: date | None, end_date: date | None,
) -> tuple[date | None, date | None]:
    """Pick the active window from the (days, start_date, end_date) trio.

    Precedence:
      1. Explicit range (`start_date` AND `end_date`) — wins outright.
      2. `days` — resolves to [today - days, today].
      3. Otherwise — open window (None, None) meaning "all time".

    Returns `(window_start, window_end)`. Either side may be None if only
    one bound is supplied (callers should treat None as "no bound on that
    side"). This is the single source of truth used by every filtered
    endpoint so the API behaves consistently.
    """
    if start_date is not None or end_date is not None:
        return (start_date, end_date)
    if days is not None and days > 0:
        # Inclusive of today. `days=N` means exactly N calendar days ending
        # today (today inclusive), so subtract N-1, not N. Audit found
        # list/stats was off-by-one vs. timeseries which used N-1.
        return (date.today() - timedelta(days=days - 1), date.today())
    return (None, None)


def _salt_key_window_clause(
    salt_key_start: date | None, salt_key_end: date | None,
):
    """Build a SQL clause that windows by parsed salt_key_receipt date.

    `salt_key_receipt` is stored as TEXT in two formats:
      * `dd-MMM-yy` (e.g. `21-May-26`) — what the poller writes after
        canonicalization (seeded rows + post-2026 sheet rows)
      * `dd-MMM-yyyy` (e.g. `21-May-2026`) — what older sheet ingest wrote
        before we tightened the date canonicalization
    The CASE picks the right `TO_DATE` format string per row; rows with
    blank or unparseable salt_key are excluded by the `safe_parsed IS
    NOT NULL` guard. Returns None when no bound is set.
    """
    if salt_key_start is None and salt_key_end is None:
        return None
    col = models.EasebuzzOnboarding.salt_key_receipt
    safe_parsed = case(
        (col.op("~")(r"^[0-9]{1,2}-[A-Za-z]{3}-[0-9]{4}$"),
         func.to_date(col, "DD-Mon-YYYY")),
        (col.op("~")(r"^[0-9]{1,2}-[A-Za-z]{3}-[0-9]{2}$"),
         func.to_date(col, "DD-Mon-YY")),
        else_=None,
    )
    conds = [safe_parsed.is_not(None)]
    if salt_key_start is not None:
        conds.append(safe_parsed >= salt_key_start)
    if salt_key_end is not None:
        conds.append(safe_parsed <= salt_key_end)
    return and_(*conds)


def _numeric_range_clause(col, min_v: int | None, max_v: int | None):
    """Build a SQL clause that filters `col` (a TEXT day-count) into the
    [min_v, max_v] integer range. Returns None if no bound is set.

    The columns we care about (`time_taken_by_eb`, `salt_key_from_docs_recd`,
    `salt_key_from_kickstart`) are TEXT to mirror the sheet, so we have to:
      * exclude blanks and non-numeric text via `col ~ '^[0-9]+$'`
      * CAST what remains to INTEGER so the >=/<= compare is numeric, not
        lexicographic (otherwise '10' < '2').
    """
    if min_v is None and max_v is None:
        return None
    safe_int = case(
        (col.op("~")(r"^[0-9]+$"), cast(col, Integer)),
        else_=None,
    )
    conds = [safe_int.isnot(None)]
    if min_v is not None:
        conds.append(safe_int >= min_v)
    if max_v is not None:
        conds.append(safe_int <= max_v)
    return and_(*conds)


def _apply_filters(query, *, q, onboarding_status, delayed,
                   days, start_date=None, end_date=None,
                   eb_days_min=None, eb_days_max=None,
                   docs_sk_min=None, docs_sk_max=None,
                   ks_sk_min=None,  ks_sk_max=None,
                   salt_key_start=None, salt_key_end=None):
    if q:
        query = query.where(models.EasebuzzOnboarding.merchant_name.ilike(f"%{q}%"))
    if onboarding_status:
        query = query.where(models.EasebuzzOnboarding.onboarding_status == onboarding_status)
    if delayed:
        query = query.where(
            or_(
                models.EasebuzzOnboarding.delay_at_gk.ilike("y%"),
                models.EasebuzzOnboarding.delay_by_merchant.ilike("y%"),
            )
        )
    # `days` / `start_date` / `end_date` window the view by
    # kickstart_date_parsed (most representative of merchant activity).
    # Rows with NULL kickstart_date_parsed are excluded from windowed views
    # — we can't bucket them in time.
    win_start, win_end = _resolve_window(days, start_date, end_date)
    if win_start is not None:
        query = query.where(models.EasebuzzOnboarding.kickstart_date_parsed >= win_start)
    if win_end is not None:
        query = query.where(models.EasebuzzOnboarding.kickstart_date_parsed <= win_end)

    eb_clause = _numeric_range_clause(
        models.EasebuzzOnboarding.time_taken_by_eb, eb_days_min, eb_days_max,
    )
    if eb_clause is not None:
        query = query.where(eb_clause)
    docs_clause = _numeric_range_clause(
        models.EasebuzzOnboarding.salt_key_from_docs_recd, docs_sk_min, docs_sk_max,
    )
    if docs_clause is not None:
        query = query.where(docs_clause)
    ks_clause = _numeric_range_clause(
        models.EasebuzzOnboarding.salt_key_from_kickstart, ks_sk_min, ks_sk_max,
    )
    if ks_clause is not None:
        query = query.where(ks_clause)

    sk_window = _salt_key_window_clause(salt_key_start, salt_key_end)
    if sk_window is not None:
        query = query.where(sk_window)

    return query


@router.get("", response_model=schemas.EasebuzzListOut)
async def list_easebuzz(
    q: str | None = Query(None, description="Search merchant_name"),
    onboarding_status: str | None = Query(None, alias="status"),
    delayed: bool | None = Query(None),
    days: int | None = Query(None, ge=1, le=3650,
                              description="Only rows whose kickstart is within the last N days"),
    start_date: date | None = Query(None, alias="start_date",
                                    description="Inclusive lower bound on kickstart_date_parsed (YYYY-MM-DD). Wins over `days` when paired with `end_date`."),
    end_date: date | None = Query(None, alias="end_date",
                                  description="Inclusive upper bound on kickstart_date_parsed (YYYY-MM-DD)."),
    eb_days_min: int | None = Query(None, ge=0, description="Min time_taken_by_eb (days, inclusive)"),
    eb_days_max: int | None = Query(None, ge=0, description="Max time_taken_by_eb (days, inclusive)"),
    docs_sk_min: int | None = Query(None, ge=0, description="Min salt_key_from_docs_recd (days, inclusive)"),
    docs_sk_max: int | None = Query(None, ge=0, description="Max salt_key_from_docs_recd (days, inclusive)"),
    ks_sk_min: int | None  = Query(None, ge=0, description="Min salt_key_from_kickstart (days, inclusive)"),
    ks_sk_max: int | None  = Query(None, ge=0, description="Max salt_key_from_kickstart (days, inclusive)"),
    salt_key_start: date | None = Query(None, description="Inclusive lower bound on salt_key_receipt (YYYY-MM-DD)"),
    salt_key_end:   date | None = Query(None, description="Inclusive upper bound on salt_key_receipt (YYYY-MM-DD)"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    filter_kwargs = dict(
        q=q, onboarding_status=onboarding_status, delayed=delayed,
        days=days, start_date=start_date, end_date=end_date,
        eb_days_min=eb_days_min, eb_days_max=eb_days_max,
        docs_sk_min=docs_sk_min, docs_sk_max=docs_sk_max,
        ks_sk_min=ks_sk_min,     ks_sk_max=ks_sk_max,
        salt_key_start=salt_key_start, salt_key_end=salt_key_end,
    )
    # Count with the same filters (before limit/offset) for "X of N" display.
    count_q = _apply_filters(
        select(func.count(models.EasebuzzOnboarding.id)),
        **filter_kwargs,
    )
    total = (await db.execute(count_q)).scalar_one()

    # Order:
    #   1. Seeded rows ("Needs review") float to the top so ops sees them first.
    #   2. Within each source, "freshness":
    #        - Seeded rows: by `created_at` DESC — newly-imported merchants
    #          surface immediately, even when the Kickoff API hasn't yet
    #          returned a kickstart_date for them (which is common because
    #          the Kickoff API lags behind the Submerchant sheet).
    #        - Sheet/dashboard rows: by `kickstart_date_parsed` DESC — the
    #          natural ordering for the historical backfill.
    #      Wrapped in a CASE so seeded and sheet rows each use the date
    #      column that's actually populated for them.
    #   3. `last_synced_at` DESC as a final stable tiebreaker.
    seeded_order = case(
        (models.EasebuzzOnboarding.source == "seeded",
         models.EasebuzzOnboarding.created_at),
        else_=None,
    )
    sheet_order = case(
        (models.EasebuzzOnboarding.source != "seeded",
         models.EasebuzzOnboarding.kickstart_date_parsed),
        else_=None,
    )
    rows_q = _apply_filters(
        select(models.EasebuzzOnboarding),
        **filter_kwargs,
    ).order_by(
        (models.EasebuzzOnboarding.source == "seeded").desc(),
        seeded_order.desc().nullslast(),
        sheet_order.desc().nullslast(),
        desc(models.EasebuzzOnboarding.last_synced_at),
    ).limit(limit).offset(offset)
    rows = (await db.execute(rows_q)).scalars().all()

    return schemas.EasebuzzListOut(
        total=total,
        rows=[easebuzz_out(e) for e in rows],
    )


# ---------------------------------------------------------------------------
# Export — CSV of the same filtered view as /api/easebuzz. No pagination:
# every matching row is emitted so the downloaded file matches what the
# user is looking at after applying their filters. Streamed so large
# downloads (10k+ rows) don't build the whole file in memory.
# ---------------------------------------------------------------------------
EXPORT_COLUMNS = [
    ("mid",                     "MID"),
    ("merchant_name",           "Merchant Name"),
    ("merchant_size",           "Size"),
    ("onboarding_status",       "Status"),
    ("source",                  "Source"),
    ("kickstart_date",          "Kickstart Date"),
    ("docs_received_date",      "Docs Received Date"),
    ("kyc_completed_by_ops",    "KYC Completed By Ops"),
    ("date_email_sent_to_eb",   "Email Sent To EB"),
    ("salt_key_receipt",        "Salt & Key Receipt"),
    ("time_taken_by_eb",        "EB Days"),
    ("salt_key_from_docs_recd", "Docs → S&K (days)"),
    ("salt_key_from_kickstart", "Kickstart → S&K (days)"),
    ("delay_at_gk",             "Delay at GK"),
    ("delay_by_merchant",       "Delay by Merchant"),
    ("reasons_for_delay_in_eb", "Reasons for Delay in EB"),
    ("ops_remarks",             "Ops Remarks"),
    ("last_synced_at",          "Last Synced"),
]


@router.get("/needs-review")
async def needs_review(
    limit: int = Query(50, ge=1, le=500, description="Max items to return"),
    db: AsyncSession = Depends(get_db),
):
    """Seeded rows that still need a kickstart OR salt&key.

    Replaces the old client-side filter on /api/easebuzz?limit=200 which
    silently capped at 200 (so the Dashboard's "Needs review" tile would
    under-count once the backlog exceeded that). This endpoint does the
    seeded + incomplete predicate in SQL, returns the full count, and
    only sends back `limit` items for any preview UI that needs them.
    """
    incomplete = and_(
        models.EasebuzzOnboarding.source == "seeded",
        or_(
            models.EasebuzzOnboarding.kickstart_date.is_(None),
            func.trim(models.EasebuzzOnboarding.kickstart_date) == "",
            models.EasebuzzOnboarding.salt_key_receipt.is_(None),
            func.trim(models.EasebuzzOnboarding.salt_key_receipt) == "",
        ),
    )
    total = (await db.execute(
        select(func.count(models.EasebuzzOnboarding.id)).where(incomplete)
    )).scalar_one()
    rows_q = (
        select(models.EasebuzzOnboarding)
        .where(incomplete)
        .order_by(desc(models.EasebuzzOnboarding.created_at))
        .limit(limit)
    )
    rows = (await db.execute(rows_q)).scalars().all()
    return {
        "total": total,
        "items": [easebuzz_out(e) for e in rows],
    }


@router.get("/export.csv")
async def export_easebuzz_csv(
    q: str | None = Query(None, description="Search merchant_name"),
    onboarding_status: str | None = Query(None, alias="status"),
    delayed: bool | None = Query(None),
    days: int | None = Query(None, ge=1, le=3650),
    start_date: date | None = Query(None, alias="start_date"),
    end_date:   date | None = Query(None, alias="end_date"),
    eb_days_min: int | None = Query(None, ge=0),
    eb_days_max: int | None = Query(None, ge=0),
    docs_sk_min: int | None = Query(None, ge=0),
    docs_sk_max: int | None = Query(None, ge=0),
    ks_sk_min:   int | None = Query(None, ge=0),
    ks_sk_max:   int | None = Query(None, ge=0),
    salt_key_start: date | None = Query(None),
    salt_key_end:   date | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Download the filter-matching rows as CSV.

    Mirrors `list_easebuzz` filter semantics exactly so what the user sees
    in the table is what they get in the file. `merchant_id` is joined to
    `merchants` to surface the MID (the human-readable id Ops uses to
    cross-reference with the spreadsheet).
    """
    filter_kwargs = dict(
        q=q, onboarding_status=onboarding_status, delayed=delayed,
        days=days, start_date=start_date, end_date=end_date,
        eb_days_min=eb_days_min, eb_days_max=eb_days_max,
        docs_sk_min=docs_sk_min, docs_sk_max=docs_sk_max,
        ks_sk_min=ks_sk_min,     ks_sk_max=ks_sk_max,
        salt_key_start=salt_key_start, salt_key_end=salt_key_end,
    )

    seeded_order = case(
        (models.EasebuzzOnboarding.source == "seeded",
         models.EasebuzzOnboarding.created_at),
        else_=None,
    )
    sheet_order = case(
        (models.EasebuzzOnboarding.source != "seeded",
         models.EasebuzzOnboarding.kickstart_date_parsed),
        else_=None,
    )

    # LEFT JOIN merchants so we can surface the MID column. We use an
    # outerjoin because some seeded rows are linked but a merchant_id of
    # NULL is theoretically possible (ON DELETE SET NULL on the FK).
    rows_q = (
        _apply_filters(
            select(models.EasebuzzOnboarding, models.Merchant.mid)
            .join(
                models.Merchant,
                models.EasebuzzOnboarding.merchant_id == models.Merchant.id,
                isouter=True,
            ),
            **filter_kwargs,
        )
        .order_by(
            (models.EasebuzzOnboarding.source == "seeded").desc(),
            seeded_order.desc().nullslast(),
            sheet_order.desc().nullslast(),
            desc(models.EasebuzzOnboarding.last_synced_at),
        )
    )
    result = await db.execute(rows_q)
    rows = result.all()  # list of Row(EasebuzzOnboarding, mid)

    def stream():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([h for _, h in EXPORT_COLUMNS])
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)

        for row in rows:
            eb, mid = row
            row_values = []
            for field, _ in EXPORT_COLUMNS:
                if field == "mid":
                    row_values.append(mid or "")
                elif field == "last_synced_at":
                    v = getattr(eb, field, None)
                    row_values.append(v.isoformat() if v else "")
                else:
                    v = getattr(eb, field, None)
                    row_values.append("" if v is None else str(v))
            writer.writerow(row_values)
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

    filename = f"easebuzz-onboarding-{date.today().isoformat()}.csv"
    return StreamingResponse(
        stream(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/eb-times")
async def eb_times(
    days: int | None = Query(30, ge=1, le=365,
                             description="Window in days ending today; ignored when start_date+end_date set"),
    start_date: date | None = Query(None, description="Inclusive lower bound (YYYY-MM-DD)"),
    end_date:   date | None = Query(None, description="Inclusive upper bound (YYYY-MM-DD)"),
    sla_days: int = Query(2, ge=0, le=90,
                          description="SLA threshold — ≤ this many days is 'fast' (green)"),
    include_seeded: bool = Query(True, description=(
        "Include seeded ('Needs review') rows. Default true — once a seeded "
        "row picks up both kickstart and salt&key its `time_taken_by_eb` is "
        "a real EB processing-time measurement and belongs on this chart. "
        "Pass false to restore the old sheet-only behavior."
    )),
    db: AsyncSession = Depends(get_db),
):
    """Per-merchant "Time taken by EB" for the chosen window.

    Returns one item per merchant whose kickstart falls inside the window
    AND has a parseable `time_taken_by_eb` value. The window is either an
    explicit [start_date, end_date] or — if those are absent — the
    trailing `days` window ending today.
    """
    win_start, win_end = _resolve_window(days, start_date, end_date)
    q = (
        select(
            models.EasebuzzOnboarding.id,
            models.EasebuzzOnboarding.merchant_name,
            models.EasebuzzOnboarding.merchant_size,
            models.EasebuzzOnboarding.time_taken_by_eb,
            models.EasebuzzOnboarding.date_email_sent_to_eb,
            models.EasebuzzOnboarding.salt_key_receipt,
            models.EasebuzzOnboarding.kickstart_date,
            models.EasebuzzOnboarding.kickstart_date_parsed,
        )
        .where(models.EasebuzzOnboarding.time_taken_by_eb.is_not(None))
        .where(models.EasebuzzOnboarding.time_taken_by_eb != "")
        .order_by(models.EasebuzzOnboarding.kickstart_date_parsed.desc())
    )
    if not include_seeded:
        q = q.where(models.EasebuzzOnboarding.source != "seeded")
    if win_start is not None:
        q = q.where(models.EasebuzzOnboarding.kickstart_date_parsed >= win_start)
    if win_end is not None:
        q = q.where(models.EasebuzzOnboarding.kickstart_date_parsed <= win_end)
    rows = (await db.execute(q)).all()

    items: list[dict] = []
    for r in rows:
        n = _to_int_or_none(r.time_taken_by_eb)
        if n is None:
            continue
        items.append({
            "id": str(r.id),
            "merchant_name": r.merchant_name,
            "merchant_size": r.merchant_size,
            "days": n,
            "is_fast": n <= sla_days,
            "email_date": r.date_email_sent_to_eb,
            "sk_date": r.salt_key_receipt,
            "kickstart_date": r.kickstart_date,
        })
    fast = sum(1 for it in items if it["is_fast"])
    slow = len(items) - fast
    return {
        "sla_days": sla_days,
        "window_start": win_start.isoformat() if win_start else None,
        "window_end":   win_end.isoformat()   if win_end   else None,
        "total":  len(items),
        "fast":   fast,
        "slow":   slow,
        "items":  items,
    }


@router.get("/eb-times/analytics", response_model=schemas.EbAnalyticsOut)
async def eb_times_analytics(
    days: int | None = Query(30, ge=1, le=365,
                             description="Window in days ending today; ignored when start_date+end_date set"),
    start_date: date | None = Query(None, description="Inclusive lower bound (YYYY-MM-DD)"),
    end_date:   date | None = Query(None, description="Inclusive upper bound (YYYY-MM-DD)"),
    sla_days: int = Query(2, ge=0, le=90,
                          description="SLA threshold — ≤ this many days is 'fast'"),
    merchant_size: str | None = Query(
        None,
        description="Optional filter, e.g. 'Emerging' / 'Emerging - Custom' / 'SME'",
    ),
    include_seeded: bool = Query(True, description=(
        "Include seeded ('Needs review') rows. Default true — these rows "
        "now carry real EB processing times after auto-derive. Pass false "
        "to restore the old sheet-only behavior."
    )),
    db: AsyncSession = Depends(get_db),
):
    """Rich analytics bundle for the EB-times detail page.

    Single round-trip producing summary stats, daily timeseries, size split,
    distribution histogram, and the full per-merchant list (sorted slowest first
    so the page can highlight top offenders without a second sort).
    """
    win_start, win_end = _resolve_window(days, start_date, end_date)
    end = win_end or date.today()
    start = win_start or (end - timedelta(days=29))  # default = last 30 days

    q = (
        select(
            models.EasebuzzOnboarding.id,
            models.EasebuzzOnboarding.merchant_name,
            models.EasebuzzOnboarding.merchant_size,
            models.EasebuzzOnboarding.time_taken_by_eb,
            models.EasebuzzOnboarding.date_email_sent_to_eb,
            models.EasebuzzOnboarding.salt_key_receipt,
            models.EasebuzzOnboarding.kickstart_date,
            models.EasebuzzOnboarding.kickstart_date_parsed,
        )
        .where(models.EasebuzzOnboarding.kickstart_date_parsed >= start)
        .where(models.EasebuzzOnboarding.kickstart_date_parsed <= end)
        .where(models.EasebuzzOnboarding.time_taken_by_eb.is_not(None))
        .where(models.EasebuzzOnboarding.time_taken_by_eb != "")
    )
    if merchant_size:
        q = q.where(models.EasebuzzOnboarding.merchant_size == merchant_size)
    if not include_seeded:
        q = q.where(models.EasebuzzOnboarding.source != "seeded")

    rows = (await db.execute(q)).all()

    # Materialize parsed rows once — every aggregate below reads from this list.
    items: list[dict] = []
    for r in rows:
        n = _to_int_or_none(r.time_taken_by_eb)
        if n is None:
            continue
        items.append({
            "id": str(r.id),
            "merchant_name": r.merchant_name,
            "merchant_size": r.merchant_size,
            "days": n,
            "is_fast": n <= sla_days,
            "email_date": r.date_email_sent_to_eb,
            "sk_date": r.salt_key_receipt,
            "kickstart_date": r.kickstart_date,
            "_kickstart_parsed": r.kickstart_date_parsed,
        })

    numeric = [it["days"] for it in items]
    total = len(items)
    fast = sum(1 for it in items if it["is_fast"])
    slow = total - fast

    # ---- Summary stats. Quantiles need a minimum sample size; return None
    # for percentiles we don't have enough data to compute reliably.
    def _q(values: list[int], n: int, k: int) -> float | None:
        if len(values) < n:
            return None
        return round(statistics.quantiles(values, n=n)[k], 2)

    stats_out = schemas.EbStats()
    if numeric:
        stats_out = schemas.EbStats(
            mean=round(statistics.fmean(numeric), 2),
            median=round(statistics.median(numeric), 2),
            p25=_q(numeric, 100, 24),
            p75=_q(numeric, 100, 74),
            p90=_q(numeric, 100, 89),
            p99=_q(numeric, 100, 98),
            min=min(numeric),
            max=max(numeric),
            stddev=round(statistics.pstdev(numeric), 2) if len(numeric) >= 2 else 0.0,
        )

    # ---- Timeseries: bucket fast/slow per kickstart day, zero-fill gaps.
    daily: dict[date, dict[str, int]] = {}
    for it in items:
        d = it["_kickstart_parsed"]
        if d is None:
            continue
        bucket = daily.setdefault(d, {"fast": 0, "slow": 0})
        bucket["fast" if it["is_fast"] else "slow"] += 1

    timeseries: list[schemas.EbTimeseriesPoint] = []
    cur = start
    while cur <= end:
        b = daily.get(cur, {"fast": 0, "slow": 0})
        timeseries.append(schemas.EbTimeseriesPoint(
            date=cur.isoformat(), fast=b["fast"], slow=b["slow"],
        ))
        cur += timedelta(days=1)

    # ---- By-size: group on merchant_size (treat blank/None as (unspecified)).
    by_size_groups: dict[str, list[dict]] = {}
    for it in items:
        key = (it["merchant_size"] or "").strip() or "(unspecified)"
        by_size_groups.setdefault(key, []).append(it)

    by_size: list[schemas.EbBySizeRow] = []
    for size_key, group in by_size_groups.items():
        nums = [g["days"] for g in group]
        by_size.append(schemas.EbBySizeRow(
            size=size_key,
            count=len(group),
            fast=sum(1 for g in group if g["is_fast"]),
            slow=sum(1 for g in group if not g["is_fast"]),
            median=round(statistics.median(nums), 2) if nums else None,
            mean=round(statistics.fmean(nums), 2) if nums else None,
        ))
    by_size.sort(key=lambda r: r.count, reverse=True)

    # ---- Distribution: 0d..14d then 15+d, including zero-count buckets so
    # the histogram bars are uniform.
    bucket_counts: Counter[str] = Counter()
    for n in numeric:
        bucket_counts["15+d" if n >= 15 else f"{n}d"] += 1

    distribution: list[schemas.EbDistributionBucket] = []
    for d_i in range(15):
        key = f"{d_i}d"
        distribution.append(schemas.EbDistributionBucket(
            day_bucket=key,
            count=bucket_counts.get(key, 0),
            is_fast=d_i <= sla_days,
        ))
    distribution.append(schemas.EbDistributionBucket(
        day_bucket="15+d",
        count=bucket_counts.get("15+d", 0),
        is_fast=15 <= sla_days,
    ))

    # Strip internal-only key; emit ALL items, slowest first.
    items_sorted = sorted(items, key=lambda it: it["days"], reverse=True)
    items_out = [
        schemas.EbItem(
            id=it["id"],
            merchant_name=it["merchant_name"],
            merchant_size=it["merchant_size"],
            days=it["days"],
            is_fast=it["is_fast"],
            email_date=it["email_date"],
            sk_date=it["sk_date"],
            kickstart_date=it["kickstart_date"],
        )
        for it in items_sorted
    ]

    return schemas.EbAnalyticsOut(
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        sla_days=sla_days,
        total=total,
        fast=fast,
        slow=slow,
        stats=stats_out,
        timeseries=timeseries,
        by_size=by_size,
        distribution=distribution,
        items=items_out,
    )


@router.get("/timeseries")
async def easebuzz_timeseries(
    days: int | None = Query(30, ge=1, le=365,
                             description="Window in days, ending today; ignored when start_date+end_date set"),
    start_date: date | None = Query(None, description="Inclusive lower bound (YYYY-MM-DD)"),
    end_date:   date | None = Query(None, description="Inclusive upper bound (YYYY-MM-DD)"),
    include_seeded: bool = Query(True, description=(
        "Include seeded ('Needs review') rows. Default true since seeded "
        "rows whose status auto-flipped to Yes/Live are legit kickoffs and "
        "should appear in the trend chart. Pass false to restore the old "
        "behavior of counting sheet-imported rows only."
    )),
    db: AsyncSession = Depends(get_db),
):
    """Daily merchant kickoff volume + approved subset, for a trend chart.

    Returns one row per calendar day in the chosen window. Days with no
    activity are still emitted (count=0) so the chart's x-axis stays uniform.
    Approval here means onboarding_status ∈ {Yes, Live}.
    """
    win_start, win_end = _resolve_window(days, start_date, end_date)
    # `timeseries` always needs concrete bounds to fill calendar days.
    # Both endpoints rely on `_resolve_window` for the same definition
    # of "last N days" (N inclusive days ending today) so the chart's
    # range matches `stats.total` for the same selection.
    end = win_end or date.today()
    start = win_start or (end - timedelta(days=29))  # default = last 30 days

    q = (
        select(
            models.EasebuzzOnboarding.kickstart_date_parsed.label("d"),
            func.count().label("count"),
            func.sum(
                case(
                    (models.EasebuzzOnboarding.onboarding_status.in_(["Yes", "Live"]), 1),
                    else_=0,
                )
            ).label("approved"),
        )
        .where(models.EasebuzzOnboarding.kickstart_date_parsed >= start)
        .where(models.EasebuzzOnboarding.kickstart_date_parsed <= end)
        .group_by(models.EasebuzzOnboarding.kickstart_date_parsed)
        .order_by(models.EasebuzzOnboarding.kickstart_date_parsed)
    )
    if not include_seeded:
        q = q.where(models.EasebuzzOnboarding.source != "seeded")
    rows = (await db.execute(q)).all()
    by_day = {r.d: {"count": r.count, "approved": int(r.approved or 0)} for r in rows}

    # Fill missing days with zeros so the chart is uniform.
    out = []
    cur = start
    while cur <= end:
        bucket = by_day.get(cur, {"count": 0, "approved": 0})
        out.append({
            "date": cur.isoformat(),
            "count": bucket["count"],
            "approved": bucket["approved"],
        })
        cur += timedelta(days=1)
    return out


@router.get("/stats", response_model=schemas.StatsOut)
async def easebuzz_stats(
    q: str | None = Query(None, description="Search merchant_name"),
    onboarding_status: str | None = Query(None, alias="status"),
    delayed: bool | None = Query(None),
    days: int | None = Query(None, ge=1, le=3650,
                             description="Window in days ending today; ignored when start_date+end_date set"),
    start_date: date | None = Query(None, description="Inclusive lower bound (YYYY-MM-DD)"),
    end_date:   date | None = Query(None, description="Inclusive upper bound (YYYY-MM-DD)"),
    eb_days_min: int | None = Query(None, ge=0),
    eb_days_max: int | None = Query(None, ge=0),
    docs_sk_min: int | None = Query(None, ge=0),
    docs_sk_max: int | None = Query(None, ge=0),
    ks_sk_min: int | None  = Query(None, ge=0),
    ks_sk_max: int | None  = Query(None, ge=0),
    salt_key_start: date | None = Query(None, description="Inclusive lower bound on salt_key_receipt (YYYY-MM-DD)"),
    salt_key_end:   date | None = Query(None, description="Inclusive upper bound on salt_key_receipt (YYYY-MM-DD)"),
    include_seeded: bool = Query(False, description=(
        "By default `stats` excludes seeded ('Needs review') rows so the "
        "headline approval rate isn't dragged down by them. Pass true to "
        "include them — used by the Onboarding page when its filter view "
        "is supposed to cover every row, seeded or not."
    )),
    db: AsyncSession = Depends(get_db),
):
    """Summary stats. By default excludes `source='seeded'` so the Dashboard's
    approval-rate KPIs aren't skewed by Needs-review rows; the Onboarding
    page passes `include_seeded=true` for filtered-subset analytics that
    cover every row.
    """
    filter_kwargs = dict(
        q=q, onboarding_status=onboarding_status, delayed=delayed,
        days=days, start_date=start_date, end_date=end_date,
        eb_days_min=eb_days_min, eb_days_max=eb_days_max,
        docs_sk_min=docs_sk_min, docs_sk_max=docs_sk_max,
        ks_sk_min=ks_sk_min,     ks_sk_max=ks_sk_max,
        salt_key_start=salt_key_start, salt_key_end=salt_key_end,
    )

    def with_seeded_guard(query):
        if include_seeded:
            return query
        return query.where(models.EasebuzzOnboarding.source != "seeded")

    count_q = with_seeded_guard(
        _apply_filters(select(func.count(models.EasebuzzOnboarding.id)), **filter_kwargs)
    )
    total = (await db.execute(count_q)).scalar_one()

    breakdown_q = with_seeded_guard(
        _apply_filters(
            select(models.EasebuzzOnboarding.onboarding_status, func.count())
            .group_by(models.EasebuzzOnboarding.onboarding_status),
            **filter_kwargs,
        )
    )
    by_status_rows = (await db.execute(breakdown_q)).all()

    # Speed breakdown defaults to non-seeded (intermediate workflow
    # durations are only well-defined for sheet/dashboard rows); when
    # include_seeded is on we keep seeded rows in too.
    speed_q = with_seeded_guard(
        _apply_filters(
            select(
                models.EasebuzzOnboarding.time_taken_by_eb,
                models.EasebuzzOnboarding.salt_key_from_docs_recd,
                models.EasebuzzOnboarding.salt_key_from_kickstart,
            ),
            **filter_kwargs,
        )
    )
    speed_rows = (await db.execute(speed_q)).all()

    speed = schemas.SpeedBreakdown(
        time_taken_by_eb=_speed_metric([r.time_taken_by_eb for r in speed_rows]),
        salt_key_from_docs_recd=_speed_metric([r.salt_key_from_docs_recd for r in speed_rows]),
        salt_key_from_kickstart=_speed_metric([r.salt_key_from_kickstart for r in speed_rows]),
    )

    return schemas.StatsOut(
        total=total,
        by_status=[
            {"status": s or "(blank)", "count": c} for s, c in by_status_rows
        ],
        speed=speed,
    )


@router.get("/{row_id}", response_model=schemas.EasebuzzOut)
async def get_easebuzz(row_id: str, db: AsyncSession = Depends(get_db)):
    row = (
        await db.execute(
            select(models.EasebuzzOnboarding)
            .where(models.EasebuzzOnboarding.id == uuid.UUID(row_id))
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Easebuzz row not found")
    return easebuzz_out(row)


@router.patch("/{row_id}", response_model=schemas.EasebuzzOut)
async def patch_easebuzz(
    row_id: str,
    body: schemas.EasebuzzPatch,
    db: AsyncSession = Depends(get_db),
):
    # Local imports to avoid pulling sync poller deps at module import time.
    from app.poller.poll import (
        _parse_sheet_date,
        compute_days_kickstart_to_salt_key,
        compute_business_days,
        _load_holidays,
        _seeded_status,
    )

    row = (
        await db.execute(
            select(models.EasebuzzOnboarding)
            .where(models.EasebuzzOnboarding.id == uuid.UUID(row_id))
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Easebuzz row not found")
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        return easebuzz_out(row)
    for k, v in updates.items():
        setattr(row, k, v)
    # Keep parsed kickstart in sync when the text value changes.
    if "kickstart_date" in updates:
        row.kickstart_date_parsed = _parse_sheet_date(updates["kickstart_date"] or "")

    # CASCADE RECOMPUTE — when any underlying date column changes, refresh
    # every dependent column in the same PATCH so the row's derived
    # numbers stay consistent without waiting for the next cron. Matches
    # the rules in _normalize_seeded_status so manual edits and the
    # automation produce identical results for the same inputs.
    DATE_INPUT_FIELDS = {
        "kickstart_date",
        "docs_received_date",
        "kyc_completed_by_ops",
        "date_email_sent_to_eb",
        "salt_key_receipt",
    }
    if DATE_INPUT_FIELDS & set(updates.keys()):
        ks  = (row.kickstart_date or "").strip() or None
        dox = (row.docs_received_date or "").strip() or None
        em  = (row.date_email_sent_to_eb or "").strip() or None
        sk  = (row.salt_key_receipt or "").strip() or None

        # Docs → S&K  (calendar days, absolute)
        new_docs_sk = compute_days_kickstart_to_salt_key(dox, sk)
        if new_docs_sk is not None:
            row.salt_key_from_docs_recd = new_docs_sk

        # EB days  (business days between email-to-EB and S&K, weekends + holidays excluded)
        new_eb_days = compute_business_days(em, sk, _load_holidays())
        if new_eb_days is not None:
            row.time_taken_by_eb = new_eb_days

        # K → S&K  (|kickstart - S&K| in calendar days MINUS EB days, clamped ≥0)
        raw_ks_sk = compute_days_kickstart_to_salt_key(ks, sk)
        if raw_ks_sk is not None and new_eb_days is not None:
            try:
                row.salt_key_from_kickstart = str(max(0, int(raw_ks_sk) - int(new_eb_days)))
            except ValueError:
                row.salt_key_from_kickstart = raw_ks_sk
        elif raw_ks_sk is not None:
            row.salt_key_from_kickstart = raw_ks_sk

        # Auto-flip onboarding_status ONLY for seeded rows — the same rule
        # the poller's normalize step uses. Sheet/dashboard rows keep
        # whatever status they had (Ops's word is final on those).
        if row.source == "seeded":
            row.onboarding_status = _seeded_status(ks, sk)
    # Edit-protection rule: only promote a seeded row to source='dashboard'
    # when the user touches a field that the automations would otherwise
    # write to (i.e. the dates the Kickoff API + Slack fill in, or the
    # status that the normalize step computes). Editing free-text fields
    # like ops_remarks or delay flags must NOT block the hourly Kickoff
    # API refetch or the daily Slack salt&key sync — otherwise a single
    # "ops remark" entry on a Needs-review row permanently freezes its
    # automation queue. Bug found in audit (2026-05-26).
    AUTOMATION_FIELDS = {
        "kickstart_date",
        "kickstart_date_parsed",
        "salt_key_receipt",
        "salt_key_from_kickstart",
        "salt_key_from_docs_recd",
        "time_taken_by_eb",
        "onboarding_status",
        "docs_received_date",
        "kyc_completed_by_ops",
        "date_email_sent_to_eb",
    }
    if AUTOMATION_FIELDS & set(updates.keys()):
        row.source = "dashboard"
    row.last_edited_in_dashboard_at = datetime.now(timezone.utc)
    row.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return easebuzz_out(row)
