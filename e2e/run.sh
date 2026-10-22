#!/usr/bin/env bash
# One command: install, run the live Studionet end-to-end test, and serve the
# live report (open port 8080 in Codespaces).
#   bash e2e/run.sh              # live on Studionet
#   bash e2e/run.sh --local      # offline dry run on the real GenLayer Python SDK
set -euo pipefail
cd "$(dirname "$0")"
[ -d node_modules ] || npm install --no-audit --no-fund --silent
node run.mjs --serve "$@"
