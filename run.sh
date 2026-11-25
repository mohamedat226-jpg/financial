#!/usr/bin/env bash
# Start the AI Audit Dashboard on http://localhost:${PORT:-8000}
set -euo pipefail
cd "$(dirname "$0")"
exec python -m uvicorn audit_app.api:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
