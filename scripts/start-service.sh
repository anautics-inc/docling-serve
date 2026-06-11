#!/usr/bin/env bash
# pm2 entrypoint for docling-serve.
#
# Single worker on purpose: the deep-extraction task store is in-memory, so a
# second worker would answer /status/poll for tasks it has never seen.
# Supervised by pm2 (process name: docling-serve) so concurrent agent sessions
# stop fighting over ad-hoc nohup restarts:
#   pm2 restart docling-serve   # the ONLY sanctioned way to restart it
set -euo pipefail

cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

exec .venv/bin/docling-serve run --host 127.0.0.1 --port 3060 --workers 1
