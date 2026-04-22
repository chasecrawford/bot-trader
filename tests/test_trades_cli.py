"""
Tests for trades_cli.py — summary, list, export subcommands.

Seeds a temp SQLite DB via TradeLog, then drives the CLI's main() with
argv and captures stdout.
"""

import io
import os
import tempfile
from contextlib import redirect_stdout
from datetime import datetime

import pytest

import trades_cli
from trade_log import TradeLog, TradeRecord


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #
@pytest.fixture
def seeded_db():
    """Create a temp DB with a known set of trades, returning its path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    log = TradeLog(path)
    base = datetime(2024, 6, 1, 10, 0, 0)
    log.record(TradeRecord(
        symbol="AAPL", entry_time=base,
        exit_time=datetime(2024, 6, 1, 12, 0, 0),
        entry_price=100, exit_price=110, qty=10, pnl=100.0,
        reason="profit target", source="live",
    ))
    log.record(TradeRecord(
        symbol="AAPL", entry_time=datetime(2024, 6, 5, 10, 0, 0),
        exit_time=datetime(2024, 6, 5, 15, 0, 0),
        entry_price=110, exit_price=105, qty=10, pnl=-50.0,
        reason="hard stop", source="live",
    ))
    log.record(TradeRecord(
        symbol="MSFT", entry_time=datetime(2024, 6, 10, 10, 0, 0),
        exit_time=datetime(2024, 6, 10, 14, 0, 0),
        entry_price=300, exit_price=315, qty=5, pnl=75.0,
        reason="bearish cross", source="backtest", run_id="bt-001",
    ))
    try:
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _run(argv):
    """Call trades_cli.main with argv and return (exit_code, stdout)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = trades_cli.main(argv)
    return code, buf.getvalue()


# --------------------------------------------------------------------------- #
# summary                                                                     #
# --------------------------------------------------------------------------- #
class TestSummary:
    def test_overall_summary(self, seeded_db):
        code, out = _run(["summary", "--db", seeded_db])
        assert code == 0
        assert "Trades:    3" in out
        assert "$+125.00" in out  # 100 - 50 + 75

    def test_filter_by_source(self, seeded_db):
        code, out = _run(["summary", "--db", seeded_db, "--source", "backtest"])
        assert code == 0
        assert "Trades:    1" in out
        assert "$+75.00" in out

    def test_filter_by_symbol(self, seeded_db):
        code, out = _run(["summary", "--db", seeded_db, "--symbol", "AAPL"])
        assert code == 0
        assert "Trades:    2" in out
        assert "$+50.00" in out  # 100 - 50

    def test_empty_result(self, seeded_db):
        code, out = _run(["summary", "--db", seeded_db, "--symbol", "NVDA"])
        assert code == 0
        assert "No trades match" in out


# --------------------------------------------------------------------------- #
# list                                                                        #
# --------------------------------------------------------------------------- #
class TestList:
    def test_list_all(self, seeded_db):
        code, out = _run(["list", "--db", seeded_db])
        assert code == 0
        assert "AAPL" in out
        assert "MSFT" in out

    def test_list_with_limit(self, seeded_db):
        code, out = _run(["list", "--db", seeded_db, "--limit", "1"])
        assert code == 0
        # Only one data row — MSFT is most recent exit
        assert "MSFT" in out
        assert out.count("\n") < 5  # header + divider + one data row + blank

    def test_list_filter_since(self, seeded_db):
        code, out = _run(["list", "--db", seeded_db, "--since", "2024-06-08"])
        assert code == 0
        assert "MSFT" in out
        assert "AAPL" not in out


# --------------------------------------------------------------------------- #
# export                                                                      #
# --------------------------------------------------------------------------- #
class TestExport:
    def test_export_creates_csv(self, seeded_db, tmp_path):
        out_path = tmp_path / "out.csv"
        code, _ = _run(["export", str(out_path), "--db", seeded_db])
        assert code == 0
        assert out_path.exists()
        contents = out_path.read_text()
        assert "symbol,entry_time" in contents  # header
        assert "AAPL" in contents
        assert "MSFT" in contents
