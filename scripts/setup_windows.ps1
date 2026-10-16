# TW Futures AI Trader - Windows one-time setup (run inside the cloned project folder):
#   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

Write-Host "== 1/5 檢查 Python / Git / GitHub CLI ==" -ForegroundColor Cyan
foreach ($pkg in @(@("python", "Python.Python.3.12"), @("git", "Git.Git"), @("gh", "GitHub.cli"))) {
  if (-not (Get-Command $pkg[0] -ErrorAction SilentlyContinue)) {
    winget install -e --id $pkg[1] --accept-package-agreements --accept-source-agreements
    $restart = $true
  }
}
if ($restart) { Write-Host "已安裝缺少的工具，請關閉並重新開啟 PowerShell 後再執行一次本腳本。" -ForegroundColor Yellow; exit 0 }

Write-Host "== 2/5 設定檔 ==" -ForegroundColor Cyan
if (-not (Test-Path "config\local.json")) { Copy-Item "config\example.json" "config\local.json" }
New-Item -ItemType Directory -Force "data", "logs" | Out-Null

Write-Host "== 3/5 GitHub 權杖（讓線上儀表板可以更新）==" -ForegroundColor Cyan
gh auth status 2>$null; if ($LASTEXITCODE -ne 0) { gh auth login }
$cfgDir = Join-Path $HOME ".config\tw-futures-ai"
New-Item -ItemType Directory -Force $cfgDir | Out-Null
(gh auth token).Trim() | Set-Content -NoNewline -Encoding ascii (Join-Path $cfgDir "gh_token")
icacls (Join-Path $cfgDir "gh_token") /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null

Write-Host "== 4/5 (選用) 永豐 Shioaji ==" -ForegroundColor Cyan
$ans = Read-Host "要安裝永豐 shioaji 套件嗎？(y/N)"
if ($ans -eq "y") { python -m pip install --user shioaji }

Write-Host "== 5/5 設定開機/登入自動執行 + 每 5 分鐘自動檢查重啟 ==" -ForegroundColor Cyan
$start = Join-Path $root "scripts\start_windows.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$start`""
$t1 = New-ScheduledTaskTrigger -AtLogOn
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 0)
Register-ScheduledTask -TaskName "TWFuturesAI" -Action $action -Trigger $t1, $t2 -Settings $settings -Force | Out-Null
& $start
Write-Host "完成！交易引擎已在背景執行。本機儀表板：python -m futures_ai dashboard → http://127.0.0.1:8766" -ForegroundColor Green
Write-Host "請把 Windows 電源設定改成『永不睡眠』，否則睡眠期間不會交易。" -ForegroundColor Yellow
