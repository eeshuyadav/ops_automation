"""Sync-runs read API.

GET-only endpoints over the `sync_runs` audit log written by the poller
(`app.poller.poll`). Uses raw psycopg to stay consistent with the writer
and to avoid the SQLAlchemy session machinery for a tiny read path.

All endpoints inherit the global `X-API-Key` gate via `app.main` — only
`/api/health` is exempt.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
from fastapi import APIRouter, HTTPException, Query, Response, status
from psycopg.rows import dict_row

from app import schemas
from app.poller.poll import _resolve_sync_url


router = APIRouter(prefix="/api/sync", tags=["sync"])


# Mirrors the column list returned to clients — keep in sync with SyncRunOut.
_RUN_COLUMNS = (
    "id, started_at, finished_at, status, "
    "gokwik_rows_seen, gokwik_new_merchants, gokwik_updated_merchants, "
    "easebuzz_rows_seen, easebuzz_new_rows, easebuzz_updated_rows, "
    "easebuzz_linked_rows, error, triggered_by"
)

# A run is considered stale (and the /api/sync/health endpoint reports 503)
# when its start is older than this OR it ended in `failed`. 8 days gives the
# weekly cron a one-day grace window for the host being briefly offline.
STALE_AFTER = timedelta(days=8)


def _connect() -> psycopg.Connection:
    """Open a short-lived psycopg connection for one request.

    Raises HTTPException 500 with a redacted message when the DSN isn't
    configured — surfacing the bare RuntimeError to clients would be noisy.
    """
    url = _resolve_sync_url()
    if not url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="DATABASE_URL / SYNC_DATABASE_URL not configured",
        )
    return psycopg.connect(url, row_factory=dict_row)


def _is_stale(started_at: datetime | None, run_status: str) -> bool:
    if run_status == "failed":
        return True
    if started_at is None:
        return True
    # psycopg returns aware datetimes for TIMESTAMPTZ; defensively coerce.
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started_at) > STALE_AFTER


def _row_to_out(row: dict) -> schemas.SyncRunOut:
    started_at = row["started_at"]
    finished_at = row["finished_at"]
    return schemas.SyncRunOut(
        id=str(row["id"]),
        started_at=started_at.isoformat() if started_at else "",
        finished_at=finished_at.isoformat() if finished_at else None,
        status=row["status"],
        gokwik_rows_seen=row["gokwik_rows_seen"] or 0,
        gokwik_new_merchants=row["gokwik_new_merchants"] or 0,
        gokwik_updated_merchants=row["gokwik_updated_merchants"] or 0,
        easebuzz_rows_seen=row["easebuzz_rows_seen"] or 0,
        easebuzz_new_rows=row["easebuzz_new_rows"] or 0,
        easebuzz_updated_rows=row["easebuzz_updated_rows"] or 0,
        easebuzz_linked_rows=row["easebuzz_linked_rows"] or 0,
        error=row["error"],
        triggered_by=row["triggered_by"],
        is_stale=_is_stale(started_at, row["status"]),
    )


@router.get("/last", response_model=schemas.SyncRunOut)
def get_last_run() -> schemas.SyncRunOut:
    """Return the most recent run row (regardless of status).

    404 when the table is empty — the dashboard treats that as "poller has
    never run; please trigger a backfill".
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {_RUN_COLUMNS} FROM sync_runs ORDER BY started_at DESC LIMIT 1"
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "No sync runs recorded yet")
    return _row_to_out(row)


@router.get("/recent", response_model=list[schemas.SyncRunOut])
def get_recent_runs(
    limit: int = Query(20, ge=1, le=100,
                       description="How many recent runs to return (max 100)"),
) -> list[schemas.SyncRunOut]:
    """Return the N most recent runs, newest first."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {_RUN_COLUMNS} FROM sync_runs "
            f"ORDER BY started_at DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
    return [_row_to_out(r) for r in rows]


@router.get("/health")
def sync_health(response: Response) -> dict:
    """External-monitor endpoint. 200 when the last run was a recent success,
    503 otherwise.

    "Recent" = started within the last 8 days. The poller currently runs
    weekly so we treat anything older as a missed cron and page.
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT started_at, status, error FROM sync_runs "
            "ORDER BY started_at DESC LIMIT 1"
        )
        row = cur.fetchone()

    if row is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "no_runs", "message": "No sync runs have ever completed."}

    if _is_stale(row["started_at"], row["status"]):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "stale",
            "last_status": row["status"],
            "last_started_at": row["started_at"].isoformat() if row["started_at"] else None,
            "error": row["error"],
            "message": "Last sync is failed or older than 8 days.",
        }

    return {
        "status": "ok",
        "last_started_at": row["started_at"].isoformat() if row["started_at"] else None,
    }
