#!/usr/bin/env bash
# TW Futures AI Trader - macOS / Linux one-time setup (run inside the cloned project folder):
#   bash scripts/setup_unix.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
for cmd in python3 git gh; do command -v "$cmd" >/dev/null || { echo "請先安裝 $cmd（macOS: brew install python git gh）"; exit 1; }; done
[ -f config/local.json ] || cp config/example.json config/local.json
mkdir -p data logs ~/.config/tw-futures-ai && chmod 700 ~/.config/tw-futures-ai
gh auth status >/dev/null 2>&1 || gh auth login
(umask 077; gh auth token > ~/.config/tw-futures-ai/gh_token)
read -r -p "要安裝永豐 shioaji 套件嗎？(y/N) " ans; [ "$ans" = "y" ] && python3 -m pip install --user shioaji
LINE="*/5 * * * * $ROOT/scripts/start.sh >/dev/null 2>&1  # tw-futures-ai-trader watchdog"
(crontab -l 2>/dev/null | grep -v "tw-futures-ai-trader watchdog"; echo "$LINE") | crontab -
"$ROOT/scripts/start.sh"
echo "完成！每 5 分鐘自動檢查並重啟。macOS 請在『系統設定 > 電池/能源』關閉自動睡眠。"
