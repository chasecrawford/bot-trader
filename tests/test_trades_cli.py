"""
Tests for trades_cli.py — summary, list, export subcommands.

Seeds a temp SQLite DB via TradeLog, then drives the CLI's main() with
argv and captures stdout.
"""

import io
import json
import os
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import trades_cli
from equity_history import EquitySnapshotLog
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


# --------------------------------------------------------------------------- #
# equity-history                                                              #
# --------------------------------------------------------------------------- #
@pytest.fixture
def equity_seeded_db():
    """Temp DB seeded with 10 daily equity snapshots starting 2026-04-22,
    plus a few rows on the 'ema' sleeve for filter tests."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    log = EquitySnapshotLog(path)
    start = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(10):
        log.record(equity=5000.0 + i * 10, timestamp=start + timedelta(days=i))
    # Two ema-sleeve rows on the last two days
    log.record(equity=4500.0, sleeve="ema",
               timestamp=start + timedelta(days=8))
    log.record(equity=4540.0, sleeve="ema",
               timestamp=start + timedelta(days=9))
    try:
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


class TestEquityHistory:
    @pytest.fixture(autouse=True)
    def _disable_alpaca_by_default(self, monkeypatch):
        """Most tests in this class exercise the heartbeat fallback path. Force
        the Alpaca client to fail so behavior is deterministic regardless of
        whether real ALPACA_* env vars are loaded in the dev environment."""
        bad_rest = MagicMock(side_effect=RuntimeError("test: alpaca disabled"))
        monkeypatch.setattr(trades_cli, "REST", bad_rest)

    def test_outputs_json_to_stdout_by_default(self, equity_seeded_db):
        code, out = _run(
            ["equity-history", "--db", equity_seeded_db, "--days", "7"]
        )
        assert code == 0
        payload = json.loads(out)
        assert "start_date" in payload
        assert "as_of" in payload
        assert "snapshots" in payload
        assert isinstance(payload["snapshots"], list)

    def test_limits_to_n_most_recent_days(self, equity_seeded_db):
        code, out = _run(
            ["equity-history", "--db", equity_seeded_db, "--days", "5"]
        )
        payload = json.loads(out)
        assert len(payload["snapshots"]) == 5

    def test_snapshots_carry_date_and_equity(self, equity_seeded_db):
        code, out = _run(
            ["equity-history", "--db", equity_seeded_db, "--days", "3"]
        )
        payload = json.loads(out)
        # Last 3 of seeded 10 days: 5070, 5080, 5090
        assert [s["equity"] for s in payload["snapshots"]] == [5070.0, 5080.0, 5090.0]
        assert all("date" in s for s in payload["snapshots"])

    def test_start_date_reflects_earliest_recorded_snapshot(self, equity_seeded_db):
        code, out = _run(
            ["equity-history", "--db", equity_seeded_db, "--days", "3"]
        )
        payload = json.loads(out)
        # start_date is the earliest snapshot in the table (the experiment
        # start), not the earliest in the requested window.
        assert payload["start_date"] == "2026-04-22"

    def test_filters_by_sleeve(self, equity_seeded_db):
        code, out = _run(
            ["equity-history", "--db", equity_seeded_db,
             "--days", "7", "--sleeve", "ema"]
        )
        payload = json.loads(out)
        assert len(payload["snapshots"]) == 2
        assert [s["equity"] for s in payload["snapshots"]] == [4500.0, 4540.0]
        # start_date for ema is the earliest ema-row, not the earliest total-row
        assert payload["start_date"] == "2026-04-30"

    def test_writes_to_output_file_when_path_given(self, equity_seeded_db, tmp_path):
        out_path = tmp_path / "paper-equity.json"
        code, stdout = _run(
            ["equity-history", "--db", equity_seeded_db,
             "--days", "7", "--output", str(out_path)]
        )
        assert code == 0
        assert out_path.exists()
        payload = json.loads(out_path.read_text())
        assert len(payload["snapshots"]) == 7

    def test_empty_db_returns_empty_snapshots(self, tmp_path):
        empty_db = tmp_path / "empty.db"
        EquitySnapshotLog(str(empty_db))  # creates schema, no rows
        code, out = _run(["equity-history", "--db", str(empty_db), "--days", "7"])
        assert code == 0
        payload = json.loads(out)
        assert payload["snapshots"] == []
        assert payload["start_date"] is None

    def test_trading_days_flag_excludes_weekends(self, tmp_path):
        """--trading-days N filters Sat/Sun out — useful for chart consumers
        that don't care about the no-op weekend equity values."""
        db = tmp_path / "weekday.db"
        log = EquitySnapshotLog(str(db))
        # Mon Apr 27 through Sun May 03, 2026 — 7 consecutive days
        start = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(7):
            log.record(equity=5000.0 + i, timestamp=start + timedelta(days=i))

        code, out = _run(
            ["equity-history", "--db", str(db), "--trading-days", "5"]
        )
        assert code == 0
        payload = json.loads(out)
        assert len(payload["snapshots"]) == 5
        # Should be Mon Tue Wed Thu Fri (Apr 27 - May 1), not Sat/Sun
        dates = [s["date"] for s in payload["snapshots"]]
        assert dates == ["2026-04-27", "2026-04-28", "2026-04-29",
                         "2026-04-30", "2026-05-01"]

    def test_days_and_trading_days_are_mutually_exclusive(self, equity_seeded_db):
        """argparse rejects both flags together so callers explicitly choose."""
        # argparse calls sys.exit(2) on a mutex violation, which raises
        # SystemExit; the message goes to stderr (not captured by _run).
        with pytest.raises(SystemExit) as exc_info:
            _run(["equity-history", "--db", equity_seeded_db,
                  "--days", "5", "--trading-days", "5"])
        assert exc_info.value.code == 2

    def test_includes_current_positions_from_heartbeat(self, equity_seeded_db, tmp_path):
        """The output JSON includes a `positions` array of currently-held
        symbols, sourced from heartbeat.json's open_positions map."""
        hb_path = tmp_path / "heartbeat.json"
        hb_path.write_text(json.dumps({
            "timestamp": "2026-05-01T12:00:00+00:00",
            "status": "ok",
            "equity": 5044.82,
            "open_positions": {
                "AAPL": {"qty": 10, "entry_price": 150.0},
                "MSFT": {"qty": 5,  "entry_price": 300.0},
                "NVDA": {"qty": 3,  "entry_price": 500.0},
            },
        }))

        code, out = _run(
            ["equity-history", "--db", equity_seeded_db,
             "--days", "7", "--heartbeat", str(hb_path)]
        )
        assert code == 0
        payload = json.loads(out)
        assert "positions" in payload
        assert sorted(payload["positions"]) == ["AAPL", "MSFT", "NVDA"]

    def test_positions_empty_when_heartbeat_missing(self, equity_seeded_db, tmp_path):
        missing = tmp_path / "no-such-file.json"
        code, out = _run(
            ["equity-history", "--db", equity_seeded_db,
             "--days", "7", "--heartbeat", str(missing)]
        )
        assert code == 0
        payload = json.loads(out)
        assert payload["positions"] == []

    def test_positions_empty_when_heartbeat_malformed(self, equity_seeded_db, tmp_path):
        hb_path = tmp_path / "broken.json"
        hb_path.write_text("not valid json {")
        code, out = _run(
            ["equity-history", "--db", equity_seeded_db,
             "--days", "7", "--heartbeat", str(hb_path)]
        )
        assert code == 0
        payload = json.loads(out)
        assert payload["positions"] == []

    def test_positions_empty_when_no_open_positions_key(self, equity_seeded_db, tmp_path):
        """A heartbeat that just hasn't recorded positions yet (e.g. fresh
        run) is treated the same as no heartbeat at all."""
        hb_path = tmp_path / "heartbeat.json"
        hb_path.write_text(json.dumps({"timestamp": "...", "status": "ok"}))
        code, out = _run(
            ["equity-history", "--db", equity_seeded_db,
             "--days", "7", "--heartbeat", str(hb_path)]
        )
        assert code == 0
        payload = json.loads(out)
        assert payload["positions"] == []

    def test_positions_pulled_from_alpaca_when_available(
            self, equity_seeded_db, tmp_path, monkeypatch):
        """Alpaca is the source of truth — heartbeat is only the fallback.
        When Alpaca returns positions, the JSON reflects them even if the
        heartbeat says something different."""
        # Heartbeat says one thing...
        hb_path = tmp_path / "heartbeat.json"
        hb_path.write_text(json.dumps({
            "open_positions": {"OLD": {"qty": 1, "entry_price": 100.0}},
        }))
        # ...but Alpaca says another. Alpaca should win.
        fake_api = MagicMock()
        fake_api.list_positions.return_value = [
            SimpleNamespace(symbol=s)
            for s in ["NVDA", "AAPL", "MSFT"]
        ]
        monkeypatch.setattr(trades_cli, "REST", MagicMock(return_value=fake_api))

        code, out = _run(
            ["equity-history", "--db", equity_seeded_db,
             "--days", "7", "--heartbeat", str(hb_path)]
        )
        assert code == 0
        payload = json.loads(out)
        # Alpaca symbols, sorted, not the heartbeat's "OLD"
        assert payload["positions"] == ["AAPL", "MSFT", "NVDA"]
        assert "OLD" not in payload["positions"]

    def test_falls_back_to_heartbeat_when_alpaca_call_fails(
            self, equity_seeded_db, tmp_path, monkeypatch):
        """When the Alpaca client raises (network, auth, anything), the
        heartbeat path is used. Note: the autouse fixture already disables
        Alpaca; this test is explicit about the contract."""
        hb_path = tmp_path / "heartbeat.json"
        hb_path.write_text(json.dumps({
            "open_positions": {
                "FALLBACK": {"qty": 1, "entry_price": 100.0},
            },
        }))
        # REST() construction succeeds, but list_positions() raises.
        fake_api = MagicMock()
        fake_api.list_positions.side_effect = ConnectionError("test: dead")
        monkeypatch.setattr(trades_cli, "REST", MagicMock(return_value=fake_api))

        code, out = _run(
            ["equity-history", "--db", equity_seeded_db,
             "--days", "7", "--heartbeat", str(hb_path)]
        )
        assert code == 0
        payload = json.loads(out)
        assert payload["positions"] == ["FALLBACK"]

    def test_returns_empty_when_both_alpaca_and_heartbeat_fail(
            self, equity_seeded_db, tmp_path, monkeypatch):
        """When Alpaca is unreachable and there's no usable heartbeat,
        positions is an empty list (never null, never missing)."""
        # Alpaca disabled by autouse fixture, no heartbeat file
        missing_hb = tmp_path / "no-such-file.json"
        code, out = _run(
            ["equity-history", "--db", equity_seeded_db,
             "--days", "7", "--heartbeat", str(missing_hb)]
        )
        assert code == 0
        payload = json.loads(out)
        assert payload["positions"] == []
