#!/usr/bin/env bash
# Launch the FastAPI server. Used by the daemonized restart path.
set -e
BACKEND="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BACKEND"
set -a
[[ -f .env ]] && source .env
set +a
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8001 --no-access-log
