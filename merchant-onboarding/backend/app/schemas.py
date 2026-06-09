from __future__ import annotations

from pydantic import BaseModel


class MerchantOut(BaseModel):
    id: str
    mid: str                                  # A
    eb_go_live_date: str | None = None        # C
    kyc_spoc: str | None = None               # D
    gokwik_kyc_complete_date: str | None = None  # E
    merchant_name: str | None = None          # F
    entity_name: str | None = None            # G
    email: str | None = None                  # H
    website: str | None = None                # I
    onboarding: str | None = None             # J
    entity: str | None = None                 # K
    first_seen_at: str
    last_synced_at: str


class EasebuzzOut(BaseModel):
    id: str
    merchant_id: str | None = None
    merchant_name: str
    merchant_size: str | None = None
    onboarding_status: str | None = None
    kickstart_date: str | None = None
    kickstart_time: str | None = None
    docs_received_date: str | None = None
    docs_received_time: str | None = None
    days_taken_ks_to_ds: str | None = None
    time_taken_ks_to_ds: str | None = None
    kyc_completed_by_ops: str | None = None
    days_taken_kyc: str | None = None
    date_email_sent_to_eb: str | None = None
    salt_key_receipt: str | None = None
    time_taken_by_eb: str | None = None
    salt_key_from_docs_recd: str | None = None
    salt_key_from_kickstart: str | None = None
    reasons_for_delay_in_eb: str | None = None
    promise: str | None = None
    delivery: str | None = None
    remarks: str | None = None
    delay_at_gk: str | None = None
    delay_by_merchant: str | None = None
    ops_remarks: str | None = None
    source: str
    last_edited_in_dashboard_at: str | None = None
    last_synced_at: str


class EasebuzzPatch(BaseModel):
    onboarding_status: str | None = None
    kickstart_date: str | None = None
    docs_received_date: str | None = None
    kyc_completed_by_ops: str | None = None
    date_email_sent_to_eb: str | None = None
    salt_key_receipt: str | None = None
    promise: str | None = None
    delivery: str | None = None
    remarks: str | None = None
    delay_at_gk: str | None = None
    delay_by_merchant: str | None = None
    ops_remarks: str | None = None


class SpeedBucket(BaseModel):
    bucket: str   # "0-1d" | "2-3d" | "4-7d" | "8-14d" | "15+d" | "unknown"
    count: int


class SpeedMetric(BaseModel):
    """Distribution + headline stats for a single duration field.

    `total` is the number of rows that contributed (parseable numeric values
    only — "unknown" rows aren't counted in summary stats but show in
    `buckets` so the UI can flag missing data).

    The percentile stack is best read as: most merchants finish between
    `p25` and `p75`, the median is the typical wait, p90 is the
    near-worst-case, and `max` is the absolute tail.
    """
    total: int
    median: float | None = None
    p90: float | None = None
    # Extended stats (added later for the Onboarding filtered-subset
    # analytics card). All are optional so older callers still work.
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    p25: float | None = None
    p75: float | None = None
    buckets: list[SpeedBucket]


class SpeedBreakdown(BaseModel):
    """Three onboarding-speed metrics, side-by-side.

    Excludes `source='seeded'` rows because those only have endpoint dates
    from the Kickoff API and lack the intermediate workflow durations.
    """
    time_taken_by_eb: SpeedMetric           # col N
    salt_key_from_docs_recd: SpeedMetric    # col O
    salt_key_from_kickstart: SpeedMetric    # col P


class StatsOut(BaseModel):
    total: int
    by_status: list[dict]
    speed: SpeedBreakdown


class EasebuzzListOut(BaseModel):
    """Paginated list envelope.

    `total` is the count after filters are applied (search/status/delayed)
    but before limit/offset, so the UI can render "Showing 1-50 of N".
    """
    total: int
    rows: list[EasebuzzOut]


class EbStats(BaseModel):
    mean:   float | None = None
    median: float | None = None
    p25:    float | None = None
    p75:    float | None = None
    p90:    float | None = None
    p99:    float | None = None
    min:    int   | None = None
    max:    int   | None = None
    stddev: float | None = None


class EbTimeseriesPoint(BaseModel):
    date: str
    fast: int
    slow: int


class EbBySizeRow(BaseModel):
    size:   str
    count:  int
    fast:   int
    slow:   int
    median: float | None = None
    mean:   float | None = None


class EbDistributionBucket(BaseModel):
    day_bucket: str
    count:      int
    is_fast:    bool


class EbItem(BaseModel):
    id: str
    merchant_name: str
    merchant_size: str | None = None
    days: int
    is_fast: bool
    email_date: str | None = None
    sk_date: str | None = None
    kickstart_date: str | None = None


class EbAnalyticsOut(BaseModel):
    window_start: str
    window_end:   str
    sla_days:     int
    total:        int
    fast:         int
    slow:         int
    stats:        EbStats
    timeseries:   list[EbTimeseriesPoint]
    by_size:      list[EbBySizeRow]
    distribution: list[EbDistributionBucket]
    items:        list[EbItem]


class SyncRunOut(BaseModel):
    """One row from `sync_runs`, plus a derived `is_stale` flag.

    `is_stale` is True when the run is older than 8 days OR ended in failure.
    The dashboard uses it to surface a banner; /api/sync/health uses the same
    rule to flip 200 -> 503 for external monitors.
    """
    id: str
    started_at: str
    finished_at: str | None = None
    status: str
    gokwik_rows_seen: int
    gokwik_new_merchants: int
    gokwik_updated_merchants: int
    easebuzz_rows_seen: int
    easebuzz_new_rows: int
    easebuzz_updated_rows: int
    easebuzz_linked_rows: int
    error: str | None = None
    triggered_by: str
    is_stale: bool
