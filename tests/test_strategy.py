"""
Tests for strategy.py — indicator math and signal generation.

Uses synthetic price series so we can engineer specific crossover scenarios
without depending on any external data source.
"""

import numpy as np
import pandas as pd
import pytest

from strategy import (
    MomentumStrategy,
    Signal,
    atr,
    cross_sectional_momentum_filter,
    ema,
    market_regime_bullish,
    rsi,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def make_bars(closes):
    """Build a minimal bars DataFrame from a list of close prices."""
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="D")
    return pd.DataFrame({"close": closes}, index=idx)


def _truncate_at_cross(bars, strat, direction):
    """
    Truncate `bars` so the last row is a `direction` ('bull' or 'bear')
    crossover of the fast/slow EMAs. Returns None if no such cross exists.
    """
    df = strat.compute_indicators(bars)
    for i in range(1, len(df)):
        prev_f, prev_s = df.iloc[i - 1]["fast_ema"], df.iloc[i - 1]["slow_ema"]
        last_f, last_s = df.iloc[i]["fast_ema"], df.iloc[i]["slow_ema"]
        if direction == "bull" and prev_f <= prev_s and last_f > last_s:
            return bars.iloc[: i + 1]
        if direction == "bear" and prev_f >= prev_s and last_f < last_s:
            return bars.iloc[: i + 1]
    return None


# --------------------------------------------------------------------------- #
# Indicator math                                                              #
# --------------------------------------------------------------------------- #
class TestEMA:
    def test_constant_series_returns_same_value(self):
        series = pd.Series([10.0] * 50)
        result = ema(series, period=12)
        # EMA of a constant is that constant
        assert np.allclose(result.values, 10.0)

    def test_ema_follows_trend(self):
        series = pd.Series(np.arange(1.0, 51.0))
        result = ema(series, period=12)
        # Should be strictly increasing for a strictly increasing input
        assert all(result.diff().dropna() > 0)
        # And lag the raw series (EMA < current value during an uptrend)
        assert result.iloc[-1] < series.iloc[-1]


class TestATR:
    def test_constant_ohlc_gives_zero_atr(self):
        # A perfectly flat market (H=L=C every bar) has zero true range.
        flat = pd.Series([100.0] * 50)
        result = atr(high=flat, low=flat, close=flat, period=14)
        # First value is NaN (no prev_close); rest should be 0
        assert np.allclose(result.dropna().values, 0.0)

    def test_atr_scales_with_range(self):
        # Wider daily ranges → larger ATR. Construct two synthetic series:
        # one with H-L = 1, another with H-L = 5, same close path.
        closes = pd.Series([100.0] * 50)
        narrow_atr = atr(high=closes + 0.5, low=closes - 0.5, close=closes, period=14)
        wide_atr = atr(high=closes + 2.5, low=closes - 2.5, close=closes, period=14)
        assert wide_atr.iloc[-1] > narrow_atr.iloc[-1]
        # A 5x wider bar range should produce a ~5x ATR (up to smoothing noise)
        assert 4.5 < wide_atr.iloc[-1] / narrow_atr.iloc[-1] < 5.5

    def test_atr_uses_prev_close_gaps(self):
        # A gap up (today's low > yesterday's close) widens true range.
        # Bar 0: close 100. Bar 1: opens at 110, range 110-108. TR should be
        # max(110-108, |110-100|, |108-100|) = 10, not just 2.
        highs = pd.Series([100.0, 110.0])
        lows = pd.Series([100.0, 108.0])
        closes = pd.Series([100.0, 109.0])
        tr_series = atr(high=highs, low=lows, close=closes, period=1)
        # With period=1, ewm alpha=1 means TR passes through unchanged
        assert tr_series.iloc[1] == pytest.approx(10.0)


class TestCrossSectionalMomentum:
    @staticmethod
    def _make_bars_with_total_return(total_return, n=200):
        """Bars where the close goes from 100 to 100*(1+total_return) linearly."""
        idx = pd.date_range("2024-01-01", periods=n, freq="D")
        closes = np.linspace(100.0, 100.0 * (1 + total_return), n)
        return pd.DataFrame({"close": closes}, index=idx)

    def test_returns_top_pct_by_trailing_return(self):
        bars_by_symbol = {
            "WINR1": self._make_bars_with_total_return(1.00),  # +100%
            "WINR2": self._make_bars_with_total_return(0.50),  # +50%
            "FLAT1": self._make_bars_with_total_return(0.00),
            "FLAT2": self._make_bars_with_total_return(0.05),
            "LOSER": self._make_bars_with_total_return(-0.50),
        }
        as_of = bars_by_symbol["WINR1"].index[-1]
        top = cross_sectional_momentum_filter(
            bars_by_symbol, as_of, lookback_days=120, top_pct=0.20
        )
        # Top 20% of 5 = 1 symbol — must be the biggest gainer
        assert top == {"WINR1"}

    def test_returns_two_when_top_40pct_of_five(self):
        bars_by_symbol = {
            "A": self._make_bars_with_total_return(1.00),
            "B": self._make_bars_with_total_return(0.50),
            "C": self._make_bars_with_total_return(0.10),
            "D": self._make_bars_with_total_return(-0.10),
            "E": self._make_bars_with_total_return(-0.50),
        }
        as_of = bars_by_symbol["A"].index[-1]
        top = cross_sectional_momentum_filter(
            bars_by_symbol, as_of, lookback_days=120, top_pct=0.40
        )
        assert top == {"A", "B"}

    def test_returns_none_when_too_few_symbols(self):
        bars_by_symbol = {
            "A": self._make_bars_with_total_return(1.0),
            "B": self._make_bars_with_total_return(0.5),
        }
        as_of = bars_by_symbol["A"].index[-1]
        top = cross_sectional_momentum_filter(
            bars_by_symbol, as_of, lookback_days=120, top_pct=0.20
        )
        # < 5 rankable symbols → filter inactive
        assert top is None

    def test_returns_none_when_insufficient_history(self):
        # Only 50 bars, but ask for 120-day lookback → no symbol qualifies
        bars_by_symbol = {
            f"SYM{i}": self._make_bars_with_total_return(0.1, n=50)
            for i in range(8)
        }
        as_of = bars_by_symbol["SYM0"].index[-1]
        top = cross_sectional_momentum_filter(
            bars_by_symbol, as_of, lookback_days=120, top_pct=0.20
        )
        assert top is None

    def test_skips_symbols_without_data_at_as_of_date(self):
        # One symbol's data ends earlier than as_of; should be skipped.
        full = self._make_bars_with_total_return(1.0, n=200)
        short = self._make_bars_with_total_return(2.0, n=100)
        bars_by_symbol = {
            "FULL1": full,
            "FULL2": self._make_bars_with_total_return(0.5, n=200),
            "FULL3": self._make_bars_with_total_return(0.3, n=200),
            "FULL4": self._make_bars_with_total_return(0.2, n=200),
            "FULL5": self._make_bars_with_total_return(0.1, n=200),
            "EARLY": short,  # Highest return but ends before as_of
        }
        as_of = full.index[-1]
        top = cross_sectional_momentum_filter(
            bars_by_symbol, as_of, lookback_days=120, top_pct=0.20
        )
        # EARLY must NOT be in the top set despite having the highest return,
        # because we don't have data for it at the as_of date.
        assert top is not None
        assert "EARLY" not in top


class TestMarketRegime:
    @staticmethod
    def _bars_with_trend(start, end, n=250):
        idx = pd.date_range("2024-01-01", periods=n, freq="D")
        closes = np.linspace(start, end, n)
        return pd.DataFrame({"close": closes}, index=idx)

    def test_bullish_when_price_above_ma(self):
        # Steady uptrend → last close is well above its 200-day mean
        bars = self._bars_with_trend(start=100.0, end=200.0, n=250)
        result = market_regime_bullish(bars, bars.index[-1], ma_days=200)
        assert result is True

    def test_bearish_when_price_below_ma(self):
        # Steady downtrend → last close is well below its 200-day mean
        bars = self._bars_with_trend(start=200.0, end=100.0, n=250)
        result = market_regime_bullish(bars, bars.index[-1], ma_days=200)
        assert result is False

    def test_returns_none_when_insufficient_history(self):
        bars = self._bars_with_trend(start=100.0, end=200.0, n=50)
        result = market_regime_bullish(bars, bars.index[-1], ma_days=200)
        assert result is None

    def test_returns_none_when_as_of_not_in_index(self):
        bars = self._bars_with_trend(start=100.0, end=200.0, n=250)
        # Pick a date that doesn't exist in the index
        missing = pd.Timestamp("2030-01-01")
        result = market_regime_bullish(bars, missing, ma_days=200)
        assert result is None


class TestComputeIndicatorsATR:
    def test_atr_column_added_when_ohlc_present(self):
        rng = np.random.default_rng(0)
        closes = 100 + rng.standard_normal(50).cumsum()
        idx = pd.date_range("2024-01-01", periods=50, freq="D")
        bars = pd.DataFrame(
            {
                "close": closes,
                "high": closes + 1.0,
                "low": closes - 1.0,
            },
            index=idx,
        )
        df = MomentumStrategy().compute_indicators(bars)
        assert "atr" in df.columns
        assert not pd.isna(df["atr"].iloc[-1])

    def test_atr_column_omitted_for_close_only_bars(self):
        # Backward-compat: existing tests using make_bars (close-only) must
        # still get a usable indicator frame, just without atr.
        bars = make_bars([100.0] * 30)
        df = MomentumStrategy().compute_indicators(bars)
        assert "atr" not in df.columns


class TestRSI:
    def test_all_gains_gives_high_rsi(self):
        # Strictly increasing series → RSI should pin near 100
        series = pd.Series(np.arange(1.0, 50.0))
        result = rsi(series, period=14)
        assert result.iloc[-1] > 95.0

    def test_all_losses_gives_low_rsi(self):
        series = pd.Series(np.arange(50.0, 1.0, -1.0))
        result = rsi(series, period=14)
        assert result.iloc[-1] < 5.0

    def test_rsi_bounded_between_0_and_100(self):
        rng = np.random.default_rng(42)
        series = pd.Series(100 + rng.standard_normal(100).cumsum())
        result = rsi(series, period=14)
        assert result.min() >= 0.0
        assert result.max() <= 100.0


# --------------------------------------------------------------------------- #
# Signal generation                                                           #
# --------------------------------------------------------------------------- #
class TestMomentumStrategy:
    def test_insufficient_history_returns_none(self):
        strat = MomentumStrategy()
        # slow_period defaults to 26; need at least 28 bars
        bars = make_bars([100.0] * 10)
        assert strat.generate_signal("TEST", bars) is None

    def test_no_crossover_returns_hold(self):
        # Flat prices → EMAs converge, no cross
        strat = MomentumStrategy()
        bars = make_bars([100.0] * 60)
        result = strat.generate_signal("FLAT", bars)
        assert result is not None
        assert result.signal == Signal.HOLD

    def test_bullish_crossover_generates_buy(self):
        # Downtrend then recovery guarantees a bullish cross somewhere;
        # truncate so that cross lands on the final bar.
        closes = list(np.linspace(100, 70, 40)) + list(np.linspace(70, 130, 80))
        bars = make_bars(closes)
        strat = MomentumStrategy()
        truncated = _truncate_at_cross(bars, strat, "bull")
        assert truncated is not None, "Failed to construct a bullish crossover"
        result = strat.generate_signal("XOVR", truncated, holding=False)
        assert result is not None
        assert result.signal == Signal.BUY
        assert "crossover" in result.reason.lower()

    def test_bullish_crossover_blocked_by_overbought_rsi(self):
        # A sharp V-shaped recovery produces both a bullish cross and
        # elevated RSI. With a strict overbought threshold, the filter
        # should block the entry.
        closes = list(np.linspace(100, 70, 40)) + list(np.linspace(70, 130, 80))
        bars = make_bars(closes)
        strat = MomentumStrategy(rsi_overbought=50.0)
        truncated = _truncate_at_cross(bars, strat, "bull")
        assert truncated is not None
        # Sanity-check the RSI is actually high enough at the cross
        df = strat.compute_indicators(truncated)
        assert df.iloc[-1]["rsi"] >= 50.0, "Test setup: RSI not elevated at cross"
        result = strat.generate_signal("HOT", truncated, holding=False)
        assert result is not None
        assert result.signal == Signal.HOLD
        assert "overbought" in result.reason.lower()

    def test_bearish_crossover_when_holding_sells(self):
        # Uptrend then decline forces fast EMA to cross below slow somewhere
        closes = list(np.linspace(70, 130, 60)) + list(np.linspace(130, 70, 80))
        bars = make_bars(closes)
        strat = MomentumStrategy()
        truncated = _truncate_at_cross(bars, strat, "bear")
        assert truncated is not None, "Failed to construct a bearish crossover"
        result = strat.generate_signal("CRASH", truncated, holding=True)
        assert result is not None
        assert result.signal == Signal.SELL
        assert "bearish" in result.reason.lower()

    def test_no_bearish_cross_when_holding_returns_hold(self):
        up = list(np.linspace(70, 120, 60))
        bars = make_bars(up)
        strat = MomentumStrategy()
        result = strat.generate_signal("BULL", bars, holding=True)
        assert result is not None
        assert result.signal == Signal.HOLD

    def test_signal_result_carries_indicator_values(self):
        bars = make_bars([100.0] * 60)
        strat = MomentumStrategy()
        result = strat.generate_signal("TEST", bars)
        assert result is not None
        assert result.symbol == "TEST"
        assert result.price == pytest.approx(100.0)
        assert 0.0 <= result.rsi <= 100.0
        assert result.fast_ema == pytest.approx(100.0)
        assert result.slow_ema == pytest.approx(100.0)
