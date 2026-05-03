# scripts/ — Windows automation

> **Windows-only.** These PowerShell scripts wrap Windows Task Scheduler.
> macOS and Linux users: see "Graceful shutdown (cross-platform)" in the
> top-level `README.md` and build the equivalent with `cron`, `systemd`, or
> `launchd`. The underlying graceful-stop primitive (`logs/STOP` sentinel
> file) is in pure Python at `trader.py:642` and is fully portable — these
> scripts just provide a Windows-native scheduling wrapper around it.

## What's here

| Script | Purpose |
|---|---|
| `start_trader.ps1` | Launches `python trader.py` from the repo root. Cleans any stale `logs\STOP` on startup. |
| `stop_trader.ps1` | Creates `logs\STOP` to request a graceful exit. Re-creates it each poll cycle (so multiple instances all see it). Force-kills any survivors after 370s. |
| `register_tasks.ps1` | One-shot setup: registers `BotTrader-Start` (09:33 ET Mon-Fri) and `BotTrader-Stop` (16:02 ET Mon-Fri) in Windows Task Scheduler. Run as Administrator. |

All three resolve their repo path via `$PSScriptRoot\..`, so they work from
any clone location without editing.

## Setup (one-time)

From an **elevated** PowerShell at the repo root:

```powershell
Start-Process powershell -Verb RunAs -ArgumentList "-ExecutionPolicy Bypass -File `"$PWD\scripts\register_tasks.ps1`""
```

This creates two Task Scheduler entries that run as the current user, with
`LogonType S4U` (no stored password; runs even when the user is logged out).
Run-times are weekdays only.

To remove them later:

```powershell
Unregister-ScheduledTask -TaskName "BotTrader-Start" -Confirm:$false
Unregister-ScheduledTask -TaskName "BotTrader-Stop"  -Confirm:$false
```

## What the scripts assume

- A virtualenv at `.venv\` in the repo root (created by `python -m venv .venv`
  followed by `pip install -r requirements.txt`). `start_trader.ps1` invokes
  `.venv\Scripts\python.exe` directly — adjust the path if your venv lives
  elsewhere.
- The repo is reachable from the user account running the task.
- `logs\` exists or can be created (the scripts create it if missing).

## Building the equivalent on macOS/Linux

The two operations you need:

1. **Start at market open**: `cd /path/to/bot-trader && python trader.py`
2. **Stop at market close**: `touch /path/to/bot-trader/logs/STOP`, then wait
   ~6 minutes (one poll interval + grace), then `pkill -f trader.py` if
   anything survives.

`cron` example (US/Eastern; adjust for your TZ or use `CRON_TZ`):

```cron
33 9  * * 1-5  cd /path/to/bot-trader && /path/to/.venv/bin/python trader.py >> logs/trader.out 2>&1
2  16 * * 1-5  touch /path/to/bot-trader/logs/STOP
8  16 * * 1-5  pkill -f "python trader.py" 2>/dev/null
```

For systemd, the equivalent is a `bot-trader.service` unit + two `.timer`
units (`bot-trader-start.timer`, `bot-trader-stop.timer`). The `STOP` sentinel
file is created by an `ExecStart=/usr/bin/touch %h/bot-trader/logs/STOP` in
the stop-timer's service.
