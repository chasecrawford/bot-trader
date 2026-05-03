# register_tasks.ps1 — run ONCE as Administrator to create the two Task Scheduler
# jobs that start and stop the EMA sleeve trader on weekday market hours.
# Invoke via:  Start-Process powershell -Verb RunAs -ArgumentList "-ExecutionPolicy Bypass -File `"$PWD\scripts\register_tasks.ps1`""
# (run that command from the repo root; the script resolves its repo path automatically.)

$repo = (Resolve-Path "$PSScriptRoot\..").Path
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

Write-Host "Registering tasks for user: $user"
Write-Host "Repo: $repo"

# S4U lets the task run whether the user is logged in or not, without storing
# the password in the task XML.  RunLevel Limited = no elevation needed at runtime.
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited

$commonSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -StopIfGoingOnBatteries $false `
    -DisallowStartIfOnBatteries $false

# ── START TASK ──────────────────────────────────────────────────────────────
$startAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$repo\scripts\start_trader.ps1`"" `
    -WorkingDirectory $repo

$startTrigger = New-ScheduledTaskTrigger `
    -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "09:33"

$startSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 7) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -StopIfGoingOnBatteries $false `
    -DisallowStartIfOnBatteries $false

Register-ScheduledTask `
    -TaskName "BotTrader-Start" `
    -Action $startAction `
    -Trigger $startTrigger `
    -Principal $principal `
    -Settings $startSettings `
    -Description "Start EMA sleeve trader at 09:33 ET Mon-Fri" `
    -Force | Out-Null
Write-Host "OK  BotTrader-Start registered."

# ── STOP TASK ───────────────────────────────────────────────────────────────
$stopAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$repo\scripts\stop_trader.ps1`"" `
    -WorkingDirectory $repo

$stopTrigger = New-ScheduledTaskTrigger `
    -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "16:02"

$stopSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -StopIfGoingOnBatteries $false `
    -DisallowStartIfOnBatteries $false

Register-ScheduledTask `
    -TaskName "BotTrader-Stop" `
    -Action $stopAction `
    -Trigger $stopTrigger `
    -Principal $principal `
    -Settings $stopSettings `
    -Description "Gracefully stop EMA sleeve trader at 16:02 ET Mon-Fri" `
    -Force | Out-Null
Write-Host "OK  BotTrader-Stop registered."

# ── DUAL-MOMENTUM TASK ──────────────────────────────────────────────────────
# Daily run; the script no-ops on non-month-end days via Alpaca's calendar.
$dmAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$repo\scripts\start_dual_momentum.ps1`"" `
    -WorkingDirectory $repo

$dmTrigger = New-ScheduledTaskTrigger `
    -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "15:00"

$dmSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -StopIfGoingOnBatteries $false `
    -DisallowStartIfOnBatteries $false

Register-ScheduledTask `
    -TaskName "BotTrader-DualMomentum" `
    -Action $dmAction `
    -Trigger $dmTrigger `
    -Principal $principal `
    -Settings $dmSettings `
    -Description "Run GEM dual-momentum rebalance check at 15:00 ET Mon-Fri (no-op except month-end)" `
    -Force | Out-Null
Write-Host "OK  BotTrader-DualMomentum registered."

Write-Host ""
Write-Host "Done. Verifying..."
schtasks /Query /TN "BotTrader-Start"        /FO LIST | Select-String "Task Name|Status|Next Run"
schtasks /Query /TN "BotTrader-Stop"         /FO LIST | Select-String "Task Name|Status|Next Run"
schtasks /Query /TN "BotTrader-DualMomentum" /FO LIST | Select-String "Task Name|Status|Next Run"
