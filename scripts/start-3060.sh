#!/usr/bin/env bash
# pm2 entrypoint for docling-serve 1.24 (cap-af-jmj-docling124 worktree) on :3060.
#
# Single worker on purpose: the deep-extraction task store is in-memory, so a
# second worker would answer /status/poll for tasks it has never seen.
# Supervised by pm2 (process name: docling-serve):
#   pm2 restart docling-serve   # the ONLY sanctioned way to restart it
#
# Rollback to the previous 1.18 build:
#   pm2 delete docling-serve && cd /opt/captify-apps/docling-serve && \
#     pm2 start scripts/start-service.sh --name docling-serve
set -euo pipefail

cd "$(dirname "$0")/.."

set -a
# Shared platform config (LiteLLM transport, AWS creds, artifact storage) lives
# in the sibling 1.18 checkout's .env; the worktree .env layers 1.24 overrides.
# shellcheck disable=SC1091
source /opt/captify-apps/docling-serve/.env 2>/dev/null || true
# shellcheck disable=SC1091
source .env
set +a

# Serve on the canonical app port regardless of the worktree's dev port override.
export UVICORN_PORT=3060
exec .venv/bin/docling-serve run --host 127.0.0.1 --port 3060 --workers 1
