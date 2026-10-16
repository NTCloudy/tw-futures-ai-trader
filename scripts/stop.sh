#!/usr/bin/env bash
# Stops the engine and removes the cron watchdog.
(crontab -l 2>/dev/null | grep -v "tw-futures-ai-trader watchdog") | crontab -
pkill -f "^python3 -m futures_ai run$" && echo "已停止自動交易" || echo "引擎沒有在執行"
