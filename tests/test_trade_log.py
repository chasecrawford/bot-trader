"""
Tests for trade_log.py — SQLite round-trip, filtering, and summary stats.
"""

import os
import tempfile
from datetime import datetime, timedelta

import pytest

from trade_log import TradeLog, TradeRecord


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_db():
    """Temporary SQLite file, cleaned up after the test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _trade(symbol="AAPL", pnl=100.0, source="backtest", run_id=None,
           entry=None, exit_=None):
    entry = entry or datetime(2024, 1, 1, 10, 0, 0)
    exit_ = exit_ or entry + timedelta(hours=1)
    return TradeRecord(
        symbol=symbol,
        entry_time=entry,
        exit_time=exit_,
        entry_price=100.0,
        exit_price=100.0 + pnl / 10,
        qty=10,
        pnl=pnl,
        reason="unit test",
        source=source,
        run_id=run_id,
    )


# --------------------------------------------------------------------------- #
# Round-trip                                                                  #
# --------------------------------------------------------------------------- #
class TestRoundTrip:
    def test_write_and_read(self, tmp_db):
        log = TradeLog(tmp_db)
        log.record(_trade(symbol="MSFT", pnl=50.0))
        rows = log.all()
        assert len(rows) == 1
        assert rows[0]["symbol"] == "MSFT"
        assert rows[0]["pnl"] == 50.0
        assert rows[0]["source"] == "backtest"

    def test_record_many(self, tmp_db):
        log = TradeLog(tmp_db)
        log.record_many([_trade(), _trade(symbol="GOOGL"), _trade(symbol="NVDA")])
        assert len(log.all()) == 3

    def test_empty_db_returns_empty_list(self, tmp_db):
        log = TradeLog(tmp_db)
        assert log.all() == []

    def test_schema_idempotent(self, tmp_db):
        # Re-opening an existing DB should not wipe or error
        TradeLog(tmp_db).record(_trade())
        log2 = TradeLog(tmp_db)
        assert len(log2.all()) == 1


# --------------------------------------------------------------------------- #
# Filtering                                                                   #
# --------------------------------------------------------------------------- #
class TestFiltering:
    def test_filter_by_source(self, tmp_db):
        log = TradeLog(tmp_db)
        log.record(_trade(source="live"))
        log.record(_trade(source="backtest"))
        log.record(_trade(source="backtest"))
        assert len(log.all(source="live")) == 1
        assert len(log.all(source="backtest")) == 2

    def test_filter_by_run_id(self, tmp_db):
        log = TradeLog(tmp_db)
        log.record(_trade(run_id="run-a"))
        log.record(_trade(run_id="run-b"))
        assert len(log.all(run_id="run-a")) == 1
        assert len(log.all(run_id="run-b")) == 1


# --------------------------------------------------------------------------- #
# Summary                                                                     #
# --------------------------------------------------------------------------- #
class TestSummary:
    def test_empty_summary(self, tmp_db):
        s = TradeLog(tmp_db).summary()
        assert s["count"] == 0
        assert s["win_rate"] == 0.0
        assert s["total_pnl"] == 0.0

    def test_win_rate_and_totals(self, tmp_db):
        log = TradeLog(tmp_db)
        log.record(_trade(pnl=100.0))
        log.record(_trade(pnl=50.0))
        log.record(_trade(pnl=-30.0))
        s = log.summary()
        assert s["count"] == 3
        assert s["wins"] == 2
        assert s["losses"] == 1
        assert s["win_rate"] == pytest.approx(2 / 3)
        assert s["total_pnl"] == pytest.approx(120.0)
        assert s["avg_win"] == pytest.approx(75.0)
        assert s["avg_loss"] == pytest.approx(-30.0)

    def test_summary_respects_filter(self, tmp_db):
        log = TradeLog(tmp_db)
        log.record(_trade(pnl=100.0, source="live"))
        log.record(_trade(pnl=-50.0, source="backtest"))
        s = log.summary(source="live")
        assert s["count"] == 1
        assert s["total_pnl"] == pytest.approx(100.0)
