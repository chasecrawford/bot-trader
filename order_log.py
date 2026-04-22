"""
order_log.py -- Append-only CSV record of every order submitted by either strategy.

This is separate from trade_log.py (which records round-trip trades for the EMA
strategy). order_log records ATOMIC ORDERS: every BUY and SELL gets one row.
This works for both per-trade strategies (EMA) and rebalance strategies (DM).

Format (logs/orders.csv):
    timestamp_utc, sleeve, symbol, side, qty, price, order_id, status, reason

Columns:
    timestamp_utc  ISO 8601 UTC timestamp
    sleeve         "ema" or "dm" -- which strategy submitted
    symbol         ticker
    side           "buy" or "sell"
    qty            shares (integer or float)
    price          estimated/fill price at submission time
    order_id       Alpaca order ID, or "DRY-RUN" / "" for non-submitted
    status         "submitted" | "filled" | "canceled" | "dry-run"
    reason         human-readable context (e.g., "Hard stop hit", "Rebalance")

Use-case: tail/grep for ad-hoc analysis; load with pandas.read_csv() for reports.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


DEFAULT_LOG_PATH = "logs/orders.csv"
HEADER = [
    "timestamp_utc", "sleeve", "symbol", "side",
    "qty", "price", "order_id", "status", "reason",
]


def record_order(
    sleeve: str,
    symbol: str,
    side: str,
    qty,
    price: float,
    order_id: str = "",
    status: str = "submitted",
    reason: str = "",
    log_path: str = DEFAULT_LOG_PATH,
) -> None:
    """
    Append one row describing an order. Creates the file (with header) if needed.

    Failures are silent -- order logging must NEVER block a real trade. If you
    care about strict logging, wrap call sites and check return.
    """
    try:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(HEADER)
            writer.writerow([
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                sleeve,
                symbol,
                side.lower(),
                qty,
                f"{float(price):.4f}" if price else "",
                order_id,
                status,
                reason,
            ])
    except Exception:
        # Order logging is best-effort. Don't ever raise from here.
        pass


def read_orders(log_path: str = DEFAULT_LOG_PATH):
    """Read all order rows as a list of dicts. Returns [] if file missing."""
    path = Path(log_path)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))
