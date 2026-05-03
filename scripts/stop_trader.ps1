# stop_trader.ps1 -- launched by Task Scheduler at 16:02 ET Mon-Fri.
# Creates logs\STOP so trader.py exits cleanly on its next loop check.
# Re-creates the file each poll cycle so multiple instances all see it.
# Waits up to 370 s then force-kills any survivors.
$repo    = (Resolve-Path "$PSScriptRoot\..").Path
$logFile = "$repo\logs\stop_trader.log"
function Log {
    param($m)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts | $m" | Tee-Object -FilePath $logFile -Append
}

if (-not (Test-Path "$repo\logs")) { New-Item -Path "$repo\logs" -ItemType Directory | Out-Null }

$stopFile = "$repo\logs\STOP"
New-Item -Path $stopFile -ItemType File -Force | Out-Null
Log "Stop file created: $stopFile"

$deadline = (Get-Date).AddSeconds(370)
while ((Get-Date) -lt $deadline) {
    # Keep re-creating the stop file so each instance sees it even if
    # another instance consumed (deleted) it first.
    if (-not (Test-Path $stopFile)) {
        New-Item -Path $stopFile -ItemType File -Force | Out-Null
    }
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
             Where-Object { $_.CommandLine -like '*trader.py*' }
    if (-not $procs) { Log "All traders exited cleanly."; exit 0 }
    Start-Sleep -Seconds 10
}

# Remove stop file so next scheduled start is not blocked
Remove-Item -Path $stopFile -Force -ErrorAction SilentlyContinue

Log "Timeout elapsed -- force-killing trader process(es)."
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*trader.py*' } |
    ForEach-Object {
        $procId = $_.ProcessId
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        Log "Killed PID $procId"
    }
