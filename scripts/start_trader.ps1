# start_trader.ps1 -- launched by Task Scheduler at 09:33 ET Mon-Fri.
# Python's own RotatingFileHandler writes to logs\trader.log; no shell
# redirect needed (and it would conflict with the file handler on Windows).
$repo = "C:\Users\Chase\Repos\bot-trader"
$env:PYTHONIOENCODING = "utf-8"
Set-Location $repo
if (-not (Test-Path "logs")) { New-Item -Path "logs" -ItemType Directory | Out-Null }
& "$repo\.venv\Scripts\python.exe" "trader.py"
