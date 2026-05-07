"""
Tests for equity_history.py — daily equity snapshots in trades.db plus a
read-only wrapper over Alpaca's portfolio_history API.

The module owns two concerns:

1. EquitySnapshotLog — persistent daily equity series, idempotent per UTC
   date + sleeve so the live monitoring loop can call record() every
   heartbeat without growing the table.
2. fetch_portfolio_history — a thin wrapper over Alpaca's API that
   normalizes the response into EquitySnapshot dataclasses and drops
   pre-funding rows (Alpaca emits leading equity=0 entries for periods
   before the account was funded).
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from equity_history import (
    EquitySnapshot,
    EquitySnapshotLog,
    fetch_portfolio_history,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_db():
    """Temporary SQLite file, cleaned up after the test. Mirrors the
    pattern used by tests/test_trade_log.py — `os.remove` may fail on
    Windows if a connection lingers, so swallow OSError."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@pytest.fixture
def log(tmp_db):
    return EquitySnapshotLog(tmp_db)


def _ts(date_str: str, hour: int = 12) -> datetime:
    """Construct a UTC datetime from 'YYYY-MM-DD' for test brevity."""
    y, m, d = (int(x) for x in date_str.split("-"))
    return datetime(y, m, d, hour, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# EquitySnapshotLog.record                                                    #
# --------------------------------------------------------------------------- #
class TestRecord:
    def test_inserts_a_row_with_supplied_fields(self, log):
        ts = _ts("2026-04-22")
        inserted = log.record(equity=5000.0, timestamp=ts)
        assert inserted is True

        snaps = log.recent(days=7)
        assert len(snaps) == 1
        assert snaps[0].equity == 5000.0
        assert snaps[0].sleeve == "total"
        assert snaps[0].timestamp.date().isoformat() == "2026-04-22"

    def test_same_date_and_sleeve_is_idempotent(self, log):
        ts1 = _ts("2026-04-22", hour=10)
        ts2 = _ts("2026-04-22", hour=23)  # later same UTC date
        first = log.record(equity=5000.0, timestamp=ts1)
        second = log.record(equity=5050.0, timestamp=ts2)

        assert first is True
        assert second is False  # duplicate (date, sleeve) is silently ignored
        snaps = log.recent(days=7)
        assert len(snaps) == 1
        assert snaps[0].equity == 5000.0  # first write wins, second ignored

    def test_different_sleeves_same_date_both_persist(self, log):
        ts = _ts("2026-04-22")
        assert log.record(equity=5000.0, sleeve="total", timestamp=ts) is True
        assert log.record(equity=4500.0, sleeve="ema", timestamp=ts) is True
        assert log.record(equity=500.0, sleeve="dm", timestamp=ts) is True

        sleeves = sorted(
            log.recent(days=7, sleeve=s)[0].sleeve
            for s in ("total", "ema", "dm")
        )
        assert sleeves == ["dm", "ema", "total"]

    def test_default_timestamp_is_now_utc(self, log):
        """Calling record() without a timestamp records 'now' in UTC."""
        before = datetime.now(timezone.utc)
        log.record(equity=5000.0)
        after = datetime.now(timezone.utc)

        snapshots = log.recent(days=1)
        assert len(snapshots) == 1
        # Snapshot timestamp should fall in the [before, after] window.
        assert before.replace(microsecond=0) <= snapshots[0].timestamp <= after


# --------------------------------------------------------------------------- #
# EquitySnapshotLog.recent                                                    #
# --------------------------------------------------------------------------- #
class TestRecent:
    def test_returns_snapshots_in_ascending_date_order(self, log):
        # Seed out of order on purpose
        log.record(equity=5100.0, timestamp=_ts("2026-04-24"))
        log.record(equity=5000.0, timestamp=_ts("2026-04-22"))
        log.record(equity=5050.0, timestamp=_ts("2026-04-23"))

        snaps = log.recent(days=7)
        assert [s.equity for s in snaps] == [5000.0, 5050.0, 5100.0]

    def test_limits_to_n_most_recent_days(self, log):
        # Seed 10 sequential days starting 2026-04-22 (uses timedelta so
        # the test isn't sensitive to month-length).
        start = _ts("2026-04-22")
        for i in range(10):
            log.record(equity=float(5000 + i * 10), timestamp=start + timedelta(days=i))

        snaps = log.recent(days=7)
        assert len(snaps) == 7
        # Should be the 7 most recent, ascending
        assert snaps[0].equity == 5030.0   # day index 3 (10 - 7)
        assert snaps[-1].equity == 5090.0  # day index 9

    def test_filters_by_sleeve(self, log):
        ts = _ts("2026-04-22")
        log.record(equity=5000.0, sleeve="total", timestamp=ts)
        log.record(equity=4500.0, sleeve="ema", timestamp=ts)
        log.record(equity=500.0, sleeve="dm", timestamp=ts)

        ema_snaps = log.recent(days=7, sleeve="ema")
        assert len(ema_snaps) == 1
        assert ema_snaps[0].equity == 4500.0
        assert ema_snaps[0].sleeve == "ema"

    def test_default_sleeve_is_total(self, log):
        ts = _ts("2026-04-22")
        log.record(equity=5000.0, sleeve="total", timestamp=ts)
        log.record(equity=4500.0, sleeve="ema", timestamp=ts)

        snaps = log.recent(days=7)  # no sleeve arg
        assert len(snaps) == 1
        assert snaps[0].sleeve == "total"

    def test_empty_table_returns_empty_list(self, log):
        assert log.recent(days=7) == []


# --------------------------------------------------------------------------- #
# EquitySnapshotLog.recent — weekday_only filter                              #
# --------------------------------------------------------------------------- #
class TestRecentWeekdayOnly:
    def test_excludes_saturday_and_sunday(self, log):
        # 2026-05-01 is Friday, 05-02 Sat, 05-03 Sun, 05-04 Mon
        log.record(equity=5000.0, timestamp=_ts("2026-05-01"))
        log.record(equity=5010.0, timestamp=_ts("2026-05-02"))  # Sat
        log.record(equity=5010.0, timestamp=_ts("2026-05-03"))  # Sun
        log.record(equity=5020.0, timestamp=_ts("2026-05-04"))

        snaps = log.recent(days=10, weekday_only=True)
        assert [s.equity for s in snaps] == [5000.0, 5020.0]

    def test_limit_counts_weekdays_not_rows(self, log):
        """LIMIT N with weekday_only must return N weekdays, even if the
        underlying table has weekend rows that would otherwise consume the limit."""
        # Seed 8 consecutive days starting Mon 2026-04-27
        start = _ts("2026-04-27")  # Monday
        for i in range(8):
            log.record(equity=5000.0 + i, timestamp=start + timedelta(days=i))
        # Days are Mon Tue Wed Thu Fri Sat Sun Mon — 6 weekdays, 2 weekend

        snaps = log.recent(days=5, weekday_only=True)
        # Should return the 5 most recent weekdays: Tue Wed Thu Fri Mon
        # (skipping Sat/Sun) — order ascending
        assert len(snaps) == 5
        equities = [s.equity for s in snaps]
        # Tue=5001, Wed=5002, Thu=5003, Fri=5004, Mon=5007
        assert equities == [5001.0, 5002.0, 5003.0, 5004.0, 5007.0]

    def test_default_is_false(self, log):
        """recent() without weekday_only includes weekend snapshots."""
        log.record(equity=5000.0, timestamp=_ts("2026-05-01"))  # Fri
        log.record(equity=5010.0, timestamp=_ts("2026-05-02"))  # Sat

        snaps = log.recent(days=7)
        assert len(snaps) == 2

    def test_weekday_only_respects_sleeve_filter(self, log):
        log.record(equity=5000.0, sleeve="total", timestamp=_ts("2026-05-01"))
        log.record(equity=4500.0, sleeve="ema", timestamp=_ts("2026-05-01"))
        log.record(equity=5010.0, sleeve="total", timestamp=_ts("2026-05-02"))  # Sat

        snaps = log.recent(days=7, sleeve="ema", weekday_only=True)
        assert len(snaps) == 1
        assert snaps[0].sleeve == "ema"


# --------------------------------------------------------------------------- #
# EquitySnapshotLog.start_date                                                #
# --------------------------------------------------------------------------- #
class TestStartDate:
    def test_returns_earliest_recorded_date_for_sleeve(self, log):
        log.record(equity=5050.0, timestamp=_ts("2026-04-23"))
        log.record(equity=5000.0, timestamp=_ts("2026-04-22"))  # earlier
        log.record(equity=5100.0, timestamp=_ts("2026-04-24"))

        assert log.start_date() == "2026-04-22"

    def test_per_sleeve_independence(self, log):
        log.record(equity=5000.0, sleeve="total", timestamp=_ts("2026-04-22"))
        log.record(equity=4500.0, sleeve="ema", timestamp=_ts("2026-04-30"))

        assert log.start_date(sleeve="total") == "2026-04-22"
        assert log.start_date(sleeve="ema") == "2026-04-30"

    def test_empty_returns_none(self, log):
        assert log.start_date() is None


# --------------------------------------------------------------------------- #
# fetch_portfolio_history                                                     #
# --------------------------------------------------------------------------- #
class TestFetchPortfolioHistory:
    def test_calls_alpaca_with_daily_resolution_for_n_days(self):
        api = MagicMock()
        api.get_portfolio_history.return_value = SimpleNamespace(
            equity=[5000.0],
            timestamp=[int(_ts("2026-04-22").timestamp())],
        )

        fetch_portfolio_history(api, days=7)

        api.get_portfolio_history.assert_called_once_with(
            period="7D", timeframe="1D",
        )

    def test_returns_equity_snapshots_in_chronological_order(self):
        api = MagicMock()
        api.get_portfolio_history.return_value = SimpleNamespace(
            equity=[5000.0, 5050.0, 5100.0],
            timestamp=[
                int(_ts("2026-04-22").timestamp()),
                int(_ts("2026-04-23").timestamp()),
                int(_ts("2026-04-24").timestamp()),
            ],
        )

        snaps = fetch_portfolio_history(api, days=7)

        assert len(snaps) == 3
        assert all(isinstance(s, EquitySnapshot) for s in snaps)
        assert [s.equity for s in snaps] == [5000.0, 5050.0, 5100.0]
        assert snaps[0].timestamp.tzinfo is timezone.utc

    def test_drops_leading_zero_equity_rows(self):
        """Alpaca returns equity=0 for windows before account funding —
        those aren't real snapshots and would distort a chart."""
        api = MagicMock()
        api.get_portfolio_history.return_value = SimpleNamespace(
            equity=[0.0, 0.0, 5000.0, 5050.0],
            timestamp=[
                int(_ts("2026-04-19").timestamp()),
                int(_ts("2026-04-20").timestamp()),
                int(_ts("2026-04-21").timestamp()),
                int(_ts("2026-04-22").timestamp()),
            ],
        )

        snaps = fetch_portfolio_history(api, days=7)

        assert len(snaps) == 2
        assert [s.equity for s in snaps] == [5000.0, 5050.0]

    def test_default_sleeve_is_total(self):
        api = MagicMock()
        api.get_portfolio_history.return_value = SimpleNamespace(
            equity=[5000.0],
            timestamp=[int(_ts("2026-04-22").timestamp())],
        )

        snaps = fetch_portfolio_history(api, days=7)

        assert snaps[0].sleeve == "total"
