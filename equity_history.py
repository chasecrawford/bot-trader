"""
equity_history.py — Daily equity snapshots for the live paper-trading account.

Two pieces, deliberately decoupled:

1. `EquitySnapshotLog` — a SQLite-backed series of (date, equity, sleeve)
   rows persisted in `trades.db` (same database as `TradeLog`). The
   monitoring loop calls `record()` on every heartbeat; the underlying
   UNIQUE (date_utc, sleeve) constraint plus `INSERT OR IGNORE` makes
   that idempotent — first call of the UTC day wins, subsequent calls
   are no-ops.

2. `fetch_portfolio_history` — a thin wrapper around Alpaca's
   `/v2/account/portfolio/history` endpoint that normalizes the
   response into `EquitySnapshot` dataclasses and drops the leading
   `equity=0` rows Alpaca emits for windows before the account was
   funded.

The two are intentionally separate: the persistent log is the source
of truth for the website chart; the API fetch is for one-shot tooling
(seeding the table after a fresh deploy, comparing local vs. broker
state, etc.).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


DEFAULT_DB_PATH = "trades.db"


@dataclass
class EquitySnapshot:
    """A single (date, equity, sleeve) data point."""
    timestamp: datetime
    equity: float
    sleeve: str = "total"


SCHEMA = """
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date_utc      TEXT NOT NULL,
    timestamp_utc TEXT NOT NULL,
    equity        REAL NOT NULL,
    sleeve        TEXT NOT NULL DEFAULT 'total',
    recorded_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (date_utc, sleeve)
);

CREATE INDEX IF NOT EXISTS idx_equity_sleeve ON equity_snapshots(sleeve);
"""


class EquitySnapshotLog:
    """Persistent daily-equity series, idempotent per (UTC date, sleeve)."""

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

    def record(
        self,
        equity: float,
        sleeve: str = "total",
        timestamp: Optional[datetime] = None,
    ) -> bool:
        """Insert a snapshot. Returns True if a new row was written, False if
        a row for this (UTC date, sleeve) already existed.
        """
        ts = timestamp if timestamp is not None else datetime.now(timezone.utc)
        # Normalize naive datetimes to UTC; reject nothing — callers shouldn't
        # have to know about tz handling for a metric this simple.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        date_utc = ts.astimezone(timezone.utc).date().isoformat()

        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO equity_snapshots
                    (date_utc, timestamp_utc, equity, sleeve)
                VALUES (?, ?, ?, ?)
                """,
                (date_utc, ts.isoformat(), float(equity), sleeve),
            )
            return cur.rowcount > 0

    def start_date(self, sleeve: str = "total") -> Optional[str]:
        """Return the earliest recorded date_utc for `sleeve` as a
        'YYYY-MM-DD' string, or None if no rows exist for that sleeve.

        Useful for labeling charts as 'since <experiment-start-date>'.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MIN(date_utc) FROM equity_snapshots WHERE sleeve = ?",
                (sleeve,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def recent(
        self,
        days: int = 7,
        sleeve: str = "total",
        weekday_only: bool = False,
    ) -> List[EquitySnapshot]:
        """Return up to `days` most recent snapshots for `sleeve`,
        ordered oldest → newest so consumers can plot directly.

        When `weekday_only=True`, Saturday/Sunday rows are excluded at the
        SQL level — meaning `LIMIT N` returns N *weekdays*, not N rows of
        which some are weekend no-ops. Holiday-aware filtering would
        require a market calendar; weekday-only is a 90% approximation.
        """
        # SQLite's strftime('%w', d) returns '0' for Sunday through '6' for Saturday.
        weekday_clause = (
            "AND strftime('%w', date_utc) NOT IN ('0', '6')"
            if weekday_only else ""
        )
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT timestamp_utc, equity, sleeve
                FROM equity_snapshots
                WHERE sleeve = ?
                {weekday_clause}
                ORDER BY date_utc DESC
                LIMIT ?
                """,
                (sleeve, int(days)),
            ).fetchall()

        # Re-sort ascending for the consumer; the LIMIT had to run on DESC
        # so we'd grab the *newest* N rather than the oldest N.
        rows = list(reversed(rows))
        return [
            EquitySnapshot(
                timestamp=datetime.fromisoformat(r["timestamp_utc"]),
                equity=float(r["equity"]),
                sleeve=r["sleeve"],
            )
            for r in rows
        ]


def fetch_portfolio_history(api, days: int = 7) -> List[EquitySnapshot]:
    """Pull the last `days` of daily equity values from Alpaca's
    portfolio_history endpoint.

    Drops leading `equity=0` rows that Alpaca emits for periods before
    the account was funded — those aren't real snapshots and would
    distort a chart's vertical scale. Zeros that appear later in the
    series (e.g., fully drawn-down account) are preserved.
    """
    history = api.get_portfolio_history(period=f"{int(days)}D", timeframe="1D")
    equities = list(history.equity)
    timestamps = list(history.timestamp)

    # Find the first non-zero equity entry. Everything before that is pre-funding.
    first_real = next(
        (i for i, e in enumerate(equities) if e and e > 0),
        len(equities),  # all zeros → return empty list below
    )

    return [
        EquitySnapshot(
            timestamp=datetime.fromtimestamp(timestamps[i], tz=timezone.utc),
            equity=float(equities[i]),
            sleeve="total",
        )
        for i in range(first_real, len(equities))
    ]
