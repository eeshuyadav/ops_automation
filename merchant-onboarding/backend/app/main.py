from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import psycopg
from fastapi import Depends, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.dependencies import require_user
from app.poller.poll import _resolve_sync_url
from app.routers import auth, easebuzz, merchants, sync


logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup tasks:

      1. Warn loudly if API_KEY is empty — the whole API is unprotected then.
      2. Fail fast if FRONTEND_URL is missing — CORS needs a concrete origin.
      3. Reap any `sync_runs` rows stuck in `running` for more than 2 hours
         (a previous poller process was OOM-killed, host rebooted, etc.).
         Without this they would block /api/sync/health forever.
    """
    if not settings.jwt_secret_key:
        if settings.is_dev:
            logger.warning(
                "JWT_SECRET_KEY is empty — using an insecure dev placeholder. "
                "Set a long random string in production .env."
            )
            # Burn in a placeholder so login can still work in dev. Production
            # /api/auth/login will reject the placeholder via the next check.
            settings.jwt_secret_key = "dev-only-do-not-use-in-prod"  # noqa: S105
        else:
            raise RuntimeError(
                "JWT_SECRET_KEY must be set in production .env. Generate one "
                "with `python -c 'import secrets; print(secrets.token_urlsafe(48))'`."
            )
    if not (settings.frontend_url or "").strip():
        # CORSMiddleware with allow_origins=[""] silently accepts nothing and
        # we'd spend hours debugging — fail at boot instead.
        raise RuntimeError(
            "FRONTEND_URL must be set to the dashboard origin (e.g. "
            "http://localhost:5173)."
        )

    reaped = _reap_stale_runs()
    logger.info("stale-run reaper: marked %d stuck run(s) as failed", reaped)

    yield
    # No shutdown hooks needed — psycopg + SQLAlchemy clean up on GC.


def _reap_stale_runs() -> int:
    """Mark any `running` rows older than 2 hours as failed.

    Runs once at startup. Idempotent and side-effect-only — never raises;
    if the DB isn't reachable we just log and continue (the rest of the
    app will fail more loudly on the first real request anyway).
    """
    url = _resolve_sync_url()
    if not url:
        logger.warning("reaper: SYNC_DATABASE_URL not set, skipping")
        return 0
    try:
        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE sync_runs SET "
                "  status = 'failed', "
                "  error = 'reaper: stale run >2h', "
                "  finished_at = now() "
                "WHERE status = 'running' "
                "  AND started_at < now() - interval '2 hours'"
            )
            return cur.rowcount or 0
    except Exception as e:  # pragma: no cover - logged + swallowed by design
        logger.exception("reaper: failed to reap stale runs: %s", e)
        return 0


app = FastAPI(
    title="Merchant Onboarding API",
    description=(
        "Backend for the merchant-onboarding dashboard. Reads merchants + "
        "easebuzz onboarding rows from the local Postgres populated by the "
        "weekly poller (app.poller)."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# `allow_credentials=True` would force a non-wildcard origin and force the
# browser to ship cookies — we use a bearer-style API key instead, so cookies
# are irrelevant and credentials must stay off.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth router is exempt from require_user (the login endpoint can't
# require a logged-in user). /api/health is also intentionally exempt so
# external monitors can liveness-probe without a token.
app.include_router(auth.router)

# Every business router is gated by JWT auth. The frontend stores the
# token in localStorage and sends it as `Authorization: Bearer <token>`.
app.include_router(merchants.router, dependencies=[Depends(require_user)])
app.include_router(easebuzz.router, dependencies=[Depends(require_user)])
app.include_router(sync.router, dependencies=[Depends(require_user)])


@app.api_route("/api/health", methods=["GET", "HEAD"])
async def health() -> dict:
    """Liveness check — 200 means the FastAPI process is up.
    No DB hit, no auth. Use this for load-balancer / nginx healthchecks.
    AWS ELB probes with HEAD by default, so both verbs are accepted.
    """
    return {"status": "ok"}


@app.get("/api/health/sync")
def health_sync(response: Response) -> dict:
    """Deep / readiness check — 200 only when the latest cron sync
    finished successfully within the last 8 days. 503 otherwise, with
    a short diagnostic in the body. No auth — IT/monitoring can hit
    this from anywhere on the VPN.

    Returned fields:
      status            -- "ok" | "stale" | "no_runs"
      last_started_at   -- ISO timestamp of the most recent sync attempt
      last_status       -- success | failed | running
      message           -- human-readable detail when not ok
    """
    # Local imports to keep this module's import surface small.
    from fastapi import status as http_status
    from app.routers.sync import _connect, _is_stale

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT started_at, status, error FROM sync_runs "
            "ORDER BY started_at DESC LIMIT 1"
        )
        row = cur.fetchone()

    if row is None:
        response.status_code = http_status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "no_runs",
                "message": "No sync runs have ever completed."}

    if _is_stale(row["started_at"], row["status"]):
        response.status_code = http_status.HTTP_503_SERVICE_UNAVAILABLE
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
        "last_status": row["status"],
    }
