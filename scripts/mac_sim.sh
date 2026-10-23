#!/usr/bin/env bash
# Mac: run Nova US against the Sinopac SIMULATION account until stop_at (config/mac_sim.json).
# Keeps the Mac awake while running (caffeinate). Ctrl+C = flatten everything, cancel orders, exit.
set -euo pipefail
cd "$(dirname "$0")/.."
source ~/.venvs/shioaji/bin/activate
source ~/.config/tw-futures-ai/sinopac.env
mkdir -p logs data
echo "永豐模擬試跑：Nova 美股開盤版（到 $(python -c 'import json;print(json.load(open("config/mac_sim.json"))["stop_at"])') 自動平倉結束；Ctrl+C 會先平倉再結束）"
caffeinate -dimsu python -m futures_ai --config config/mac_sim.json run 2>&1 | tee -ia logs/mac_sim.log  # -i: tee ignores Ctrl+C so the shutdown messages are kept
