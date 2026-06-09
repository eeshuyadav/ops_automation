"""Apply schema.sql to the onboarding Postgres.

Idempotent — every CREATE uses IF NOT EXISTS. Safe to re-run.

Usage (from backend/):
    SYNC_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/merchant_onboarding \
        python scripts/init_db.py

Resolves DB URL from SYNC_DATABASE_URL or DATABASE_URL (stripping +asyncpg).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent              # backend/scripts
BACKEND = HERE.parent                                # backend
SCHEMA = BACKEND / "app" / "poller" / "schema.sql"

load_dotenv(BACKEND / ".env")


def main() -> int:
    url = (
        os.environ.get("SYNC_DATABASE_URL")
        or os.environ.get("DATABASE_URL", "").replace("+asyncpg", "")
    )
    if not url:
        print("ERROR: SYNC_DATABASE_URL / DATABASE_URL not set", file=sys.stderr)
        return 1
    if not SCHEMA.exists():
        print(f"ERROR: schema not found at {SCHEMA}", file=sys.stderr)
        return 1
    sql = SCHEMA.read_text()
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(sql)
        conn.commit()
    safe_host = url.split("@")[-1] if "@" in url else url
    print(f"Applied {SCHEMA.relative_to(BACKEND)} to {safe_host}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
