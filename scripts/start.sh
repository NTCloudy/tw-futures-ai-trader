#!/usr/bin/env bash
# Starts the engine in the background (survives terminal close). Logs: logs/engine.log
set -euo pipefail
export PATH="$HOME/.local/node/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
cd "$(dirname "$0")/.."
mkdir -p logs
if pgrep -f "^python3 -m futures_ai run$" >/dev/null; then echo "already running"; exit 0; fi
nohup setsid python3 -m futures_ai run >> logs/engine.log 2>&1 < /dev/null &
echo "started pid $!"
