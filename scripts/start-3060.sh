#!/usr/bin/env bash
# pm2 entrypoint for the current docling-serve checkout on :3060.
#
# Single worker on purpose: the deep-extraction task store is in-memory, so a
# second worker would answer /status/poll for tasks it has never seen.
# Supervised by pm2 (process name: docling-serve):
#   pm2 restart docling-serve   # the ONLY sanctioned way to restart it
#
set -euo pipefail

cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source /opt/captify-apps/docling-serve/.env 2>/dev/null || true
set +a

# Legacy Office installation is an explicit deployment step. Service startup
# never mutates the host; /ready/adapters reports legacy-office availability.

# Serve on the canonical app port regardless of the worktree's dev port override.
export UVICORN_PORT=3060
exec .venv/bin/docling-serve run --host 127.0.0.1 --port 3060 --workers 1
