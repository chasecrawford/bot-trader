"""
strategy.py — Signal generation via EMA crossover + RSI momentum filter.

The idea: go long when the fast EMA crosses above the slow EMA (momentum turning
up), but only if RSI isn't already overbought. Exit on the opposite crossover.

This is a simple, well-known framework — intentionally. Start simple, prove it
works, then iterate. Avoid the temptation to add 10 indicators before you've
validated the base case.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Set

import numpy as np
import pandas as pd

import config


class Signal(Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class SignalResult:
    symbol: str
    signal: Signal
    price: float
    rsi: float
    fast_ema: float
    slow_ema: float
    reason: str
    atr: Optional[float] = None
    # Used by TrendStateStrategy (50/200-day MAs). Optional for backward compat
    # with MomentumStrategy which only populates the EMA fields above.
    fast_ma: Optional[float] = None
    slow_ma: Optional[float] = None


# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------
def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=period, adjust=False).mean()


def atr(high: pd.Series, low: pd.Series, close: pd.Series,
        period: int = 14) -> pd.Series:
    """
    Average True Range (Wilder's smoothing).

    True Range is the largest of: (high-low), |high-prev_close|, |low-prev_close|.
    ATR is then the EMA of TR with alpha = 1/period (Wilder's method), producing
    a series in the same units as price — a typical one-bar volatility measure.

    NaN for the first bar (no prev_close); smoothing pulls in over `period` bars.
    """
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    # Wilder's smoothing is equivalent to an EMA with alpha = 1/period
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_values = 100 - (100 / (1 + rs))

    # Edge cases where rs is undefined (avg_loss == 0):
    #   - avg_gain > 0 → all gains → RSI should be 100
    #   - avg_gain == 0 → flat price → RSI neutral at 50
    # Without this, the old `fillna(50)` masked strong uptrends as neutral.
    rsi_values = rsi_values.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    rsi_values = rsi_values.mask((avg_loss == 0) & (avg_gain == 0), 50.0)
    return rsi_values.fillna(50)


# ---------------------------------------------------------------------------
# Cross-sectional momentum filter
# ---------------------------------------------------------------------------
def cross_sectional_momentum_filter(
    bars_by_symbol: Dict[str, pd.DataFrame],
    as_of: pd.Timestamp,
    lookback_days: int,
    top_pct: float,
) -> Optional[Set[str]]:
    """
    Rank symbols by trailing return as of `as_of` and return the top fraction.

    Returns a set of symbols eligible for new entries. Symbols not in the set
    are in the bottom (1 - top_pct) of momentum and should NOT be entered,
    regardless of any per-symbol time-series signal.

    Returns None when there isn't enough history to rank meaningfully — caller
    should treat that as "filter inactive" rather than "all symbols rejected"
    so a backtest doesn't go cold for its first 6 months.
    """
    returns: Dict[str, float] = {}
    for symbol, bars in bars_by_symbol.items():
        if as_of not in bars.index:
            continue
        history = bars.loc[:as_of]
        # Need at least lookback_days+1 bars to compute trailing return
        if len(history) < lookback_days + 1:
            continue
        past_price = float(history.iloc[-lookback_days - 1]["close"])
        current_price = float(history.iloc[-1]["close"])
        if past_price <= 0 or pd.isna(past_price) or pd.isna(current_price):
            continue
        returns[symbol] = (current_price - past_price) / past_price

    # Need a reasonable number of rankable symbols for the filter to be
    # meaningful. Below this we treat the filter as inactive.
    if len(returns) < 5:
        return None

    n_top = max(1, int(round(len(returns) * top_pct)))
    sorted_symbols = sorted(returns, key=returns.get, reverse=True)
    return set(sorted_symbols[:n_top])


# ---------------------------------------------------------------------------
# Market regime filter
# ---------------------------------------------------------------------------
def market_regime_bullish(
    benchmark_bars: pd.DataFrame,
    as_of: pd.Timestamp,
    ma_days: int,
) -> Optional[bool]:
    """
    Classic "price > 200-day MA" regime filter on a benchmark series.

    Returns:
      True  if benchmark close at `as_of` is above its trailing-`ma_days` SMA
      False if benchmark close is at or below its SMA (bearish regime)
      None  when there isn't enough history to decide — caller should treat
            this as "filter inactive" so backtest warmup doesn't go cold.
    """
    if as_of not in benchmark_bars.index:
        return None
    history = benchmark_bars.loc[:as_of]
    if len(history) < ma_days:
        return None
    ma = float(history["close"].iloc[-ma_days:].mean())
    last_close = float(history["close"].iloc[-1])
    return last_close > ma


# ---------------------------------------------------------------------------
# Main signal engine
# ---------------------------------------------------------------------------
class MomentumStrategy:
    """EMA crossover + RSI filter."""

    def __init__(
        self,
        fast_period: int = config.FAST_EMA,
        slow_period: int = config.SLOW_EMA,
        rsi_period: int = config.RSI_PERIOD,
        rsi_overbought: float = config.RSI_OVERBOUGHT,
        rsi_oversold: float = config.RSI_OVERSOLD,
        atr_period: int = getattr(config, "ATR_PERIOD", 14),
    ):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.rsi_period = rsi_period
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        self.atr_period = atr_period

    def compute_indicators(self, bars: pd.DataFrame) -> pd.DataFrame:
        """
        Add fast_ema, slow_ema, rsi, and (if OHLC) atr columns. Idempotent:
        skips computation for any column already present in `bars` (lets
        backtests pre-compute once on full history and avoid O(N^2) recompute).
        """
        atr_needed = (
            "atr" not in bars.columns
            and "high" in bars.columns
            and "low" in bars.columns
        )
        if (
            "fast_ema" in bars.columns
            and "slow_ema" in bars.columns
            and "rsi" in bars.columns
            and not atr_needed
        ):
            return bars
        df = bars.copy()
        if "fast_ema" not in df.columns:
            df["fast_ema"] = ema(df["close"], self.fast_period)
        if "slow_ema" not in df.columns:
            df["slow_ema"] = ema(df["close"], self.slow_period)
        if "rsi" not in df.columns:
            df["rsi"] = rsi(df["close"], self.rsi_period)
        if atr_needed:
            df["atr"] = atr(df["high"], df["low"], df["close"], self.atr_period)
        return df

    def generate_signal(
        self, symbol: str, bars: pd.DataFrame, holding: bool = False
    ) -> Optional[SignalResult]:
        """
        Evaluate the most recent bar and return a signal.

        `holding=True` means we already own the symbol, so we only look for exits.
        """
        if len(bars) < self.slow_period + 2:
            return None  # Not enough history to compute indicators reliably

        df = self.compute_indicators(bars)
        last = df.iloc[-1]
        prev = df.iloc[-2]

        # Bullish cross: fast EMA crosses from below to above slow EMA
        bullish_cross = (
            prev["fast_ema"] <= prev["slow_ema"]
            and last["fast_ema"] > last["slow_ema"]
        )
        # Bearish cross: fast EMA crosses from above to below slow EMA
        bearish_cross = (
            prev["fast_ema"] >= prev["slow_ema"]
            and last["fast_ema"] < last["slow_ema"]
        )

        last_atr = None
        if "atr" in df.columns and not pd.isna(last["atr"]):
            last_atr = float(last["atr"])

        base = dict(
            symbol=symbol,
            price=float(last["close"]),
            rsi=float(last["rsi"]),
            fast_ema=float(last["fast_ema"]),
            slow_ema=float(last["slow_ema"]),
            atr=last_atr,
        )

        if holding:
            # Only consider exits
            if bearish_cross:
                return SignalResult(
                    signal=Signal.SELL,
                    reason="Bearish EMA crossover",
                    **base,
                )
            return SignalResult(signal=Signal.HOLD, reason="Holding", **base)

        # Not holding — look for entries
        if bullish_cross:
            if last["rsi"] >= self.rsi_overbought:
                return SignalResult(
                    signal=Signal.HOLD,
                    reason=f"Crossover but RSI overbought ({last['rsi']:.1f})",
                    **base,
                )
            return SignalResult(
                signal=Signal.BUY,
                reason=f"Bullish crossover with RSI={last['rsi']:.1f}",
                **base,
            )

        return SignalResult(signal=Signal.HOLD, reason="No crossover", **base)


# ---------------------------------------------------------------------------
# Trend-state strategy — an alternative to MomentumStrategy
# ---------------------------------------------------------------------------
class TrendStateStrategy:
    """
    State-based trend-following: enter when price is above both the fast and
    slow moving averages (confirmed uptrend); exit when price drops below the
    slow MA (uptrend broken). No "event" trigger like an EMA crossover --
    instead, we evaluate the current state of the trend on every bar.

    Compared to MomentumStrategy (EMA-cross event):
      - Far less whipsaw (a single noisy bar doesn't flip the signal)
      - Stays in trends longer (doesn't exit on bearish cross during shallow pullbacks)
      - Generates fewer total trades
    """

    def __init__(
        self,
        fast_ma_period: int = getattr(config, "STATE_FAST_MA", 50),
        slow_ma_period: int = getattr(config, "STATE_SLOW_MA", 200),
        atr_period: int = getattr(config, "ATR_PERIOD", 14),
    ):
        self.fast_ma_period = fast_ma_period
        self.slow_ma_period = slow_ma_period
        self.atr_period = atr_period

    def compute_indicators(self, bars: pd.DataFrame) -> pd.DataFrame:
        """
        Add fast_ma, slow_ma, and (if OHLC) atr columns.

        Idempotent: if the columns are already present (e.g., the backtest
        precomputed them once for the whole history and now passes a slice),
        return `bars` unchanged. This avoids O(N^2) recomputation in backtests.
        """
        atr_needed = (
            "atr" not in bars.columns
            and "high" in bars.columns
            and "low" in bars.columns
        )
        if (
            "fast_ma" in bars.columns
            and "slow_ma" in bars.columns
            and not atr_needed
        ):
            return bars
        df = bars.copy()
        if "fast_ma" not in df.columns:
            df["fast_ma"] = df["close"].rolling(self.fast_ma_period).mean()
        if "slow_ma" not in df.columns:
            df["slow_ma"] = df["close"].rolling(self.slow_ma_period).mean()
        if atr_needed:
            df["atr"] = atr(df["high"], df["low"], df["close"], self.atr_period)
        return df

    def generate_signal(
        self, symbol: str, bars: pd.DataFrame, holding: bool = False
    ) -> Optional[SignalResult]:
        """
        Evaluate the current trend state and return a signal.

        Entry (not holding): price > fast_ma AND price > slow_ma
        Exit (holding): price < slow_ma  (breaks the long-term uptrend)
        """
        if len(bars) < self.slow_ma_period + 1:
            return None

        df = self.compute_indicators(bars)
        last = df.iloc[-1]
        price = float(last["close"])
        fast = float(last["fast_ma"])
        slow = float(last["slow_ma"])

        last_atr = None
        if "atr" in df.columns and not pd.isna(last["atr"]):
            last_atr = float(last["atr"])

        # Fields that don't apply to this strategy stay zero/None — kept on
        # SignalResult for backward-compat with the EMA-cross consumers.
        base = dict(
            symbol=symbol,
            price=price,
            rsi=0.0,
            fast_ema=0.0,
            slow_ema=0.0,
            atr=last_atr,
            fast_ma=fast,
            slow_ma=slow,
        )

        in_uptrend = price > fast and price > slow

        if holding:
            # Exit when price drops below the slow MA -- long-term trend broken.
            if price < slow:
                return SignalResult(
                    signal=Signal.SELL,
                    reason=f"Price ${price:.2f} below {self.slow_ma_period}DMA "
                           f"${slow:.2f} (trend broken)",
                    **base,
                )
            return SignalResult(
                signal=Signal.HOLD,
                reason=f"Holding (price > {self.slow_ma_period}DMA)",
                **base,
            )

        # Not holding -- look for entries
        if in_uptrend:
            return SignalResult(
                signal=Signal.BUY,
                reason=f"Uptrend confirmed (price > {self.fast_ma_period}DMA "
                       f"and {self.slow_ma_period}DMA)",
                **base,
            )

        return SignalResult(
            signal=Signal.HOLD,
            reason="No uptrend (price below at least one MA)",
            **base,
        )
