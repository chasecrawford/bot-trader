# start_dual_momentum.ps1 -- launched by Task Scheduler at 15:00 ET Mon-Fri.
# Runs live_dual_momentum.py once. The script self-checks Alpaca's calendar
# and exits without trading on non-month-end days, so daily invocation is safe.
# Logs go to logs\live_dual_momentum.log via the script's own RotatingFileHandler.
$repo = (Resolve-Path "$PSScriptRoot\..").Path
$env:PYTHONIOENCODING = "utf-8"
Set-Location $repo
if (-not (Test-Path "logs")) { New-Item -Path "logs" -ItemType Directory | Out-Null }
& "$repo\.venv\Scripts\python.exe" "live_dual_momentum.py"
