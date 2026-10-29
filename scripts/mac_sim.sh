#!/usr/bin/env bash
# Mac: run the AI in config/mac_sim.json against the Sinopac SIMULATION account (stop_at optional).
# Keeps the Mac awake while running (caffeinate). Ctrl+C = flatten everything, cancel orders, exit.
set -euo pipefail
cd "$(dirname "$0")/.."
source ~/.venvs/shioaji/bin/activate
source ~/.config/tw-futures-ai/sinopac.env
mkdir -p logs data
echo "永豐模擬試跑（設定：config/mac_sim.json）。結束時按一次 Ctrl+C：會先平倉、刪單再結束。"
PYTHONUNBUFFERED=1 caffeinate -dimsu python -u -m futures_ai --config config/mac_sim.json run 2>&1 | tee -ia logs/mac_sim.log  # -i: tee ignores Ctrl+C so the shutdown messages are kept
