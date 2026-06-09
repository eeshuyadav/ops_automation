#!/usr/bin/env bash
# Start the FastAPI dev server.
set -euo pipefail

cd "$(dirname "$0")"

if [[ -d .venv ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
