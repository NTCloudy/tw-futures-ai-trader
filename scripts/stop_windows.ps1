# Stops the engine and removes the scheduled task.
Unregister-ScheduledTask -TaskName "TWFuturesAI" -Confirm:$false -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "futures_ai run" -and $_.Name -match "^python" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Write-Host "已停止自動交易。"
