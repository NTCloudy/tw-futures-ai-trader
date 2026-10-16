# Starts the engine in the background if it is not already running (used by Task Scheduler).
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$running = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "futures_ai run" -and $_.Name -match "^python" }
if ($running) { exit 0 }
New-Item -ItemType Directory -Force "logs" | Out-Null
Start-Process -FilePath "pythonw" -ArgumentList "-m", "futures_ai", "run" -WorkingDirectory $root -WindowStyle Hidden `
  -RedirectStandardOutput "logs\engine.out.log" -RedirectStandardError "logs\engine.err.log"
