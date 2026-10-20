#!/usr/bin/env bash
# Starts the engine in the background (survives terminal close). Logs: logs/engine.log
set -euo pipefail
export PATH="$HOME/.local/node/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"  # Needed for systemd-run --user under cron.
cd "$(dirname "$0")/.."
mkdir -p logs
if pgrep -f "^python3 -m futures_ai run$" >/dev/null; then echo "already running"; exit 0; fi
# Prefer a separate systemd user unit so the engine does not live inside the cgroup of whatever
# launched it (e.g. an IDE / agent service) and is not killed when that service restarts.
if command -v systemd-run >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  systemd-run --user --collect --quiet --unit="tw-futures-engine-$(date +%s)" -p WorkingDirectory="$PWD" \
    --setenv=PATH="$PATH" --setenv=HOME="$HOME" \
    bash -c 'exec python3 -m futures_ai run >> logs/engine.log 2>&1 < /dev/null'
  sleep 1
  echo "started pid $(pgrep -f '^python3 -m futures_ai run$' || echo '?') (systemd user unit)"
else
  nohup setsid python3 -m futures_ai run >> logs/engine.log 2>&1 < /dev/null &
  echo "started pid $!"
fi
