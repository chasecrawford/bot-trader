"""
trade_log.py — Persistent record of every trade, live or simulated.

Uses stdlib sqlite3 so there are no extra dependencies. Both the live trader
and the backtest engine write through this module, which means you can run a
single query to see all historical activity regardless of source.

Each row is one complete round-trip (entry → exit). The `source` column
distinguishes live paper/real trades from backtest runs; `run_id` lets you
tag a backtest so you can compare parameter sweeps without them bleeding
into each other.

Basic usage:

    log = TradeLog("trades.db")
    log.record(Trade(symbol="AAPL", ...))
    rows = log.all()
    stats = log.summary()
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional


DEFAULT_DB_PATH = "trades.db"


@dataclass
class TradeRecord:
    """One completed round-trip trade."""
    symbol: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    reason: str
    source: str = "live"        # "live", "backtest", etc.
    run_id: Optional[str] = None  # Optional tag for grouping backtest runs


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL,
    entry_time   TEXT NOT NULL,
    exit_time    TEXT NOT NULL,
    entry_price  REAL NOT NULL,
    exit_price   REAL NOT NULL,
    qty          REAL NOT NULL,
    pnl          REAL NOT NULL,
    reason       TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'live',
    run_id       TEXT,
    recorded_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_source ON trades(source);
CREATE INDEX IF NOT EXISTS idx_trades_run_id ON trades(run_id);
"""


class TradeLog:
    """Thin wrapper around a SQLite trade table."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = str(db_path)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # Writes                                                             #
    # ------------------------------------------------------------------ #
    def record(self, trade: TradeRecord) -> int:
        """Insert a single trade. Returns the new row id."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO trades
                    (symbol, entry_time, exit_time, entry_price, exit_price,
                     qty, pnl, reason, source, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.symbol,
                    trade.entry_time.isoformat(),
                    trade.exit_time.isoformat(),
                    float(trade.entry_price),
                    float(trade.exit_price),
                    float(trade.qty),
                    float(trade.pnl),
                    trade.reason,
                    trade.source,
                    trade.run_id,
                ),
            )
            return cur.lastrowid

    def record_many(self, trades: Iterable[TradeRecord]) -> None:
        for t in trades:
            self.record(t)

    # ------------------------------------------------------------------ #
    # Reads                                                              #
    # ------------------------------------------------------------------ #
    def all(self, source: Optional[str] = None,
            run_id: Optional[str] = None) -> List[dict]:
        """Return every recorded trade, optionally filtered."""
        query = "SELECT * FROM trades"
        clauses, params = [], []
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY exit_time ASC, id ASC"

        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params)]

    def summary(self, source: Optional[str] = None,
                run_id: Optional[str] = None) -> dict:
        """Aggregate stats over (optionally filtered) trades."""
        rows = self.all(source=source, run_id=run_id)
        if not rows:
            return {
                "count": 0, "wins": 0, "losses": 0,
                "win_rate": 0.0, "total_pnl": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0,
            }
        wins = [r for r in rows if r["pnl"] > 0]
        losses = [r for r in rows if r["pnl"] <= 0]
        total = sum(r["pnl"] for r in rows)
        return {
            "count": len(rows),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(rows),
            "total_pnl": total,
            "avg_win": (sum(r["pnl"] for r in wins) / len(wins)) if wins else 0.0,
            "avg_loss": (sum(r["pnl"] for r in losses) / len(losses))
                        if losses else 0.0,
        }
