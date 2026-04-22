"""
Tests for risk_manager.py — position sizing, stops, and daily halts.
"""

from datetime import date

import pytest

from risk_manager import RiskManager, PositionState


# --------------------------------------------------------------------------- #
# Position sizing                                                             #
# --------------------------------------------------------------------------- #
class TestPositionSizing:
    def test_zero_price_returns_zero(self):
        rm = RiskManager()
        assert rm.calculate_position_size(equity=10_000, price=0) == 0

    def test_zero_equity_returns_zero(self):
        rm = RiskManager()
        assert rm.calculate_position_size(equity=0, price=100) == 0

    def test_position_cap_only(self):
        # 10k equity, 10% cap = $1000 notional
        rm = RiskManager(max_position_pct=0.10)
        assert rm.calculate_position_size(equity=10_000, price=100) == 1000.0

    def test_risk_cap_tighter_than_position_cap(self):
        # Risk budget 2% of 10k = $200. Stop 5% below $100 = $5/share risk.
        # Risk-based notional = 200/5 * 100 = $4,000.
        # Position cap: 10% of 10k = $1,000.
        # Min → $1,000 (position cap wins).
        rm = RiskManager(max_position_pct=0.10, max_portfolio_risk=0.02,
                         stop_loss_pct=0.05)
        stop = 100 * (1 - 0.05)
        size = rm.calculate_position_size(equity=10_000, price=100,
                                          stop_price=stop)
        assert size == 1000.0

    def test_position_cap_tighter_than_risk_cap(self):
        # Huge stop distance → risk-based cap becomes binding.
        # Price 100, stop 50 → $50/share risk. Risk budget $200 → 4 shares
        # → $400 notional. Position cap: $1000 notional. Min = $400.
        rm = RiskManager(max_position_pct=0.10, max_portfolio_risk=0.02)
        size = rm.calculate_position_size(equity=10_000, price=100,
                                          stop_price=50)
        assert size == 400.0

    def test_stop_above_price_ignored(self):
        # Nonsensical stop (above entry) shouldn't drive sizing to zero
        rm = RiskManager(max_position_pct=0.10)
        size = rm.calculate_position_size(equity=10_000, price=100,
                                          stop_price=110)
        # Falls back to position cap notional
        assert size == 1000.0

    def test_returns_fractional_when_appropriate(self):
        # 10k equity, 10% cap = $1000 notional. At $703 price (e.g. SPY-like),
        # this corresponds to 1.42 shares — a fractional position.
        rm = RiskManager(max_position_pct=0.10)
        notional = rm.calculate_position_size(equity=10_000, price=703.0)
        assert notional == 1000.0
        # The CALLER divides by price to get shares (1.42 here)
        assert notional / 703.0 == pytest.approx(1.4225, abs=0.001)


# --------------------------------------------------------------------------- #
# Entry gating                                                                #
# --------------------------------------------------------------------------- #
class TestEntryGating:
    def test_can_open_when_empty(self):
        rm = RiskManager()
        assert rm.can_open_position("AAPL") is True

    def test_cannot_open_when_already_holding(self):
        rm = RiskManager()
        rm.register_entry("AAPL", 100.0, 10)
        assert rm.can_open_position("AAPL") is False

    def test_cannot_open_when_halted(self):
        rm = RiskManager()
        rm.trading_halted = True
        assert rm.can_open_position("AAPL") is False

    def test_cannot_exceed_max_open_positions(self):
        rm = RiskManager(max_open_positions=2)
        rm.register_entry("AAPL", 100.0, 10)
        rm.register_entry("MSFT", 200.0, 5)
        assert rm.can_open_position("GOOGL") is False


# --------------------------------------------------------------------------- #
# Stop-out logic                                                              #
# --------------------------------------------------------------------------- #
class TestStops:
    def test_no_stop_when_not_holding(self):
        rm = RiskManager()
        assert rm.should_stop_out("AAPL", 100.0) is None

    def test_hard_stop_triggers(self):
        rm = RiskManager(stop_loss_pct=0.05)
        rm.register_entry("AAPL", 100.0, 10)
        # 5% below entry = 95
        assert rm.should_stop_out("AAPL", 94.99) is not None
        assert "hard stop" in rm.should_stop_out("AAPL", 94.99).lower()

    def test_no_hard_stop_above_threshold(self):
        rm = RiskManager(stop_loss_pct=0.05)
        rm.register_entry("AAPL", 100.0, 10)
        assert rm.should_stop_out("AAPL", 96.0) is None

    def test_trailing_stop_activates_after_run_up(self):
        rm = RiskManager(stop_loss_pct=0.05, trailing_stop_pct=0.03)
        rm.register_entry("AAPL", 100.0, 10)
        # Push price up — trailing stop only activates once trail > entry
        rm.should_stop_out("AAPL", 110.0)  # updates HWM to 110
        # Trail = 110 * 0.97 = 106.7, which is > entry (100), so active
        reason = rm.should_stop_out("AAPL", 106.0)
        assert reason is not None
        assert "trailing" in reason.lower()

    def test_trailing_stop_inactive_near_entry(self):
        # If HWM hasn't moved much, trailing stop shouldn't fire above hard stop
        rm = RiskManager(stop_loss_pct=0.05, trailing_stop_pct=0.03)
        rm.register_entry("AAPL", 100.0, 10)
        rm.should_stop_out("AAPL", 101.0)  # tiny run-up, HWM=101
        # Trail = 101 * 0.97 = 97.97 — which is still below entry (100), so
        # should NOT trigger a trailing exit at 98.
        assert rm.should_stop_out("AAPL", 98.0) is None


# --------------------------------------------------------------------------- #
# Daily loss halt                                                             #
# --------------------------------------------------------------------------- #
class TestDailyLoss:
    def test_start_day_sets_baseline(self):
        rm = RiskManager()
        rm.start_day(10_000, date(2024, 1, 1))
        assert rm.day_start_equity == 10_000
        assert rm.trading_halted is False

    def test_check_daily_loss_triggers_halt(self):
        rm = RiskManager(max_daily_loss_pct=0.03)
        rm.start_day(10_000, date(2024, 1, 1))
        # 3% loss exactly
        breached = rm.check_daily_loss(9_700)
        assert breached is True
        assert rm.trading_halted is True

    def test_small_loss_does_not_halt(self):
        rm = RiskManager(max_daily_loss_pct=0.03)
        rm.start_day(10_000, date(2024, 1, 1))
        assert rm.check_daily_loss(9_800) is False
        assert rm.trading_halted is False

    def test_new_day_resets_halt(self):
        rm = RiskManager(max_daily_loss_pct=0.03)
        rm.start_day(10_000, date(2024, 1, 1))
        rm.check_daily_loss(9_500)  # halt
        assert rm.trading_halted is True
        rm.start_day(9_500, date(2024, 1, 2))  # next day
        assert rm.trading_halted is False
        assert rm.day_start_equity == 9_500

    def test_check_without_start_day_returns_false(self):
        rm = RiskManager()
        assert rm.check_daily_loss(5_000) is False


# --------------------------------------------------------------------------- #
# Position state helpers                                                      #
# --------------------------------------------------------------------------- #
class TestPositionState:
    def test_high_water_only_moves_up(self):
        pos = PositionState("AAPL", entry_price=100.0, qty=10,
                            high_water_mark=100.0)
        pos.update_high_water(105.0)
        assert pos.high_water_mark == 105.0
        pos.update_high_water(102.0)
        assert pos.high_water_mark == 105.0  # Unchanged
