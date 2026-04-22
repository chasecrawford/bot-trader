"""
monitoring.py — Operational plumbing for an unattended trading bot.

Three concerns live here:

1. `setup_logging`     — stdout + rotating file handler so trader logs
                         survive a crash or restart.
2. `Heartbeat`         — periodic JSON status file so an external monitor
                         (cron, uptime checker, your phone) can verify the
                         bot is alive and see what it's doing.
3. `WebhookAlertHandler` — a logging handler that POSTs ERROR-level events
                         to a configured URL (Slack incoming webhook,
                         Discord, your own endpoint). Silent if no URL.

None of this requires third-party dependencies — stdlib only.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional


# --------------------------------------------------------------------------- #
# Logging                                                                     #
# --------------------------------------------------------------------------- #
DEFAULT_LOG_DIR = "logs"
DEFAULT_LOG_FILE = "trader.log"
DEFAULT_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    log_dir: str = DEFAULT_LOG_DIR,
    log_file: str = DEFAULT_LOG_FILE,
    level: int = logging.INFO,
    max_bytes: int = 5 * 1024 * 1024,   # 5 MB per file
    backup_count: int = 5,               # keep 5 rotated files
    webhook_url: Optional[str] = None,
) -> logging.Logger:
    """
    Configure root logging to go to both stdout and a rotating file. If
    `webhook_url` is provided, also attach a handler that POSTs ERROR
    events to that URL.

    Returns the root logger, which is pre-configured so `logging.getLogger("trader")`
    (or any other name) picks up the same handlers.
    """
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_path = Path(log_dir) / log_file

    root = logging.getLogger()
    root.setLevel(level)

    # Clear any handlers a previous call (or basicConfig) installed.
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(DEFAULT_LOG_FORMAT, DEFAULT_DATE_FORMAT)

    # Stdout
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)

    # Rotating file
    file_handler = RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count,
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Optional webhook alerts (ERROR+ only)
    if webhook_url:
        webhook = WebhookAlertHandler(webhook_url)
        webhook.setLevel(logging.ERROR)
        webhook.setFormatter(formatter)
        root.addHandler(webhook)

    return root


# --------------------------------------------------------------------------- #
# Heartbeat                                                                   #
# --------------------------------------------------------------------------- #
class Heartbeat:
    """
    Write a small JSON file on each tick so an external monitor can check
    whether the bot is alive. Overwrites the same file — you're looking
    at the latest state, not history.

    Expected monitor pattern: "mtime of this file should be < 5 minutes old
    during market hours." If it's stale, the bot is stuck.
    """

    def __init__(self, path: str = "heartbeat.json") -> None:
        self.path = Path(path)

    def write(self, **status: Any) -> None:
        """Write current status to the heartbeat file atomically.

        All keyword arguments are serialized into the JSON payload alongside
        a generated `timestamp`. Non-JSON-serializable values are stringified.
        """
        payload: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **status,
        }

        # Atomic write: write to tmp, then rename. Avoids readers seeing a
        # half-written file if they poll at exactly the wrong moment.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, default=str, indent=2))
        os.replace(tmp, self.path)

    def read(self) -> Optional[Dict[str, Any]]:
        """Return the most recent heartbeat payload, or None if missing."""
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return None


# --------------------------------------------------------------------------- #
# Webhook alerts                                                              #
# --------------------------------------------------------------------------- #
class WebhookAlertHandler(logging.Handler):
    """
    Fire-and-forget JSON POST on each log record. Designed for Slack/Discord
    incoming webhooks or your own endpoint. Failures are swallowed — a
    broken alert channel must not take down the trading loop.
    """

    def __init__(self, url: str, timeout: float = 3.0) -> None:
        super().__init__()
        self.url = url
        self.timeout = timeout

    def emit(self, record: logging.LogRecord) -> None:
        try:
            body = json.dumps({
                "text": self.format(record),
                "level": record.levelname,
                "logger": record.name,
            }).encode("utf-8")
            req = urllib.request.Request(
                self.url,
                data=body,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=self.timeout).close()
        except (urllib.error.URLError, OSError, ValueError):
            # Never raise from a logging handler.
            pass
