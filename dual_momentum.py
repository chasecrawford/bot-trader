"""
dual_momentum.py — Antonacci-style dual momentum on asset-class ETFs.

Two layered momentum filters drive allocation:
  - Relative momentum: rank a universe of risky assets by trailing N-month
    return; the top N are candidates for holding.
  - Absolute momentum: require each candidate's trailing return to exceed a
    threshold (0.0 by default). Slots that don't qualify rotate into the safe
    asset (default: AGG, US aggregate bonds).

Rebalancing is monthly. There are no per-position stops — the strategy IS its
own risk management: when an asset's momentum fades, it's replaced at the next
rebalance. Holdings are equal-weighted across the top N target slots.

This module deliberately reuses none of the EMA-cross strategy code; the two
frameworks are different enough that sharing logic creates more confusion
than it saves.

Run with:  python dual_momentum.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import config
from backtest import annualized_sharpe, max_drawdown
from data_source import get_data_source


# --------------------------------------------------------------------------- #
# Strategy logic — pure functions, easy to unit test                          #
# --------------------------------------------------------------------------- #
def trailing_returns(
    bars_by_symbol: Dict[str, pd.DataFrame],
    as_of: pd.Timestamp,
    lookback_days: int,
) -> Dict[str, float]:
    """
    Compute trailing total return (close-to-close) over `lookback_days` for
    each symbol with sufficient history at `as_of`.
    """
    out: Dict[str, float] = {}
    for symbol, bars in bars_by_symbol.items():
        if as_of not in bars.index:
            continue
        history = bars.loc[:as_of]
        if len(history) < lookback_days + 1:
            continue
        past = float(history.iloc[-lookback_days - 1]["close"])
        current = float(history.iloc[-1]["close"])
        if past <= 0 or pd.isna(past) or pd.isna(current):
            continue
        out[symbol] = (current - past) / past
    return out


def select_dual_momentum_targets(
    returns: Dict[str, float],
    risky_universe: List[str],
    safe_asset: str,
    top_n: int,
    threshold: float,
) -> Dict[str, float]:
    """
    Apply the dual momentum rules to a returns map and return target weights.

    Rules:
      1. Among risky_universe, rank by trailing return descending.
      2. The top `top_n` candidates are slot candidates.
      3. Each slot is filled by its candidate ONLY IF that candidate's return
         exceeds `threshold` (absolute momentum). Otherwise the slot rotates
         to `safe_asset`.
      4. All N slots are equal-weighted (each = 1/N).

    Returns a dict {symbol: weight} where weights sum to 1.0. If the safe
    asset receives multiple slots, its weight is summed.
    """
    risky_returns = {s: r for s, r in returns.items() if s in risky_universe}
    if not risky_returns:
        # Universe data unavailable — park everything in safe.
        return {safe_asset: 1.0}

    ranked = sorted(risky_returns.items(), key=lambda kv: kv[1], reverse=True)
    slot_weight = 1.0 / top_n
    weights: Dict[str, float] = {}

    for i in range(top_n):
        if i < len(ranked) and ranked[i][1] > threshold:
            symbol, _ = ranked[i]
            weights[symbol] = weights.get(symbol, 0.0) + slot_weight
        else:
            weights[safe_asset] = weights.get(safe_asset, 0.0) + slot_weight

    return weights


def is_month_end(date: pd.Timestamp, all_dates: List[pd.Timestamp], idx: int) -> bool:
    """True when `date` is the last trading bar of its calendar month."""
    if idx == len(all_dates) - 1:
        return True
    next_date = all_dates[idx + 1]
    return (date.year, date.month) != (next_date.year, next_date.month)


# --------------------------------------------------------------------------- #
# Monthly-rebalance backtest engine                                           #
# --------------------------------------------------------------------------- #
@dataclass
class DualMomentumBacktest:
    risky_universe: List[str] = field(
        default_factory=lambda: list(config.DUAL_MOMENTUM_RISKY))
    safe_asset: str = config.DUAL_MOMENTUM_SAFE
    lookback_days: int = config.DUAL_MOMENTUM_LOOKBACK_DAYS
    top_n: int = config.DUAL_MOMENTUM_TOP_N
    threshold: float = config.DUAL_MOMENTUM_THRESHOLD
    start: str = config.BACKTEST_START
    end: str = config.BACKTEST_END
    initial_capital: float = config.BACKTEST_INITIAL_CAPITAL
    benchmark: str = config.BENCHMARK_SYMBOL
    risk_free_rate: float = config.RISK_FREE_RATE
    data_source_name: str = config.BACKTEST_DATA_SOURCE

    cash: float = 0.0
    holdings: Dict[str, int] = field(default_factory=dict)  # symbol -> shares
    equity_curve: List[float] = field(default_factory=list)
    equity_dates: List[pd.Timestamp] = field(default_factory=list)
    trades: List[Dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cash = self.initial_capital

    # ------------------------------------------------------------------ #
    # Data                                                               #
    # ------------------------------------------------------------------ #
    def _load_data(self) -> Dict[str, pd.DataFrame]:
        """Load all needed symbols. Pull a buffer year before start so the
        first month-end of the backtest already has a 12-month lookback."""
        symbols = list({*self.risky_universe, self.safe_asset, self.benchmark})
        # Buffer = lookback + ~30 trading days slack. Convert to calendar days
        # generously; weekends/holidays mean we need more calendar than trading.
        buffer_days = int(self.lookback_days * 1.5) + 30
        buffered_start = (
            pd.Timestamp(self.start) - pd.Timedelta(days=buffer_days)
        ).strftime("%Y-%m-%d")
        source = get_data_source(self.data_source_name)
        return source.load(symbols=symbols, start=buffered_start, end=self.end)

    # ------------------------------------------------------------------ #
    # Portfolio accounting                                               #
    # ------------------------------------------------------------------ #
    def _portfolio_value(self, date: pd.Timestamp,
                         data: Dict[str, pd.DataFrame]) -> float:
        value = self.cash
        for symbol, shares in self.holdings.items():
            bars = data.get(symbol)
            if bars is None or date not in bars.index:
                continue
            value += shares * float(bars.loc[date]["close"])
        return value

    def _trade(self, date: pd.Timestamp, symbol: str, side: str,
               qty: int, price: float) -> None:
        self.trades.append({
            "date": date, "symbol": symbol, "side": side,
            "qty": qty, "price": price, "value": qty * price,
        })

    # ------------------------------------------------------------------ #
    # Rebalance — adjust current holdings to match target weights        #
    # ------------------------------------------------------------------ #
    def _rebalance(self, date: pd.Timestamp, target_weights: Dict[str, float],
                   data: Dict[str, pd.DataFrame]) -> None:
        portfolio_value = self._portfolio_value(date, data)

        # Compute target share counts from target weights.
        target_shares: Dict[str, int] = {}
        for symbol, weight in target_weights.items():
            bars = data.get(symbol)
            if bars is None or date not in bars.index:
                continue
            price = float(bars.loc[date]["close"])
            if price <= 0:
                continue
            target_shares[symbol] = int((portfolio_value * weight) // price)

        # SELLS first — frees up cash for BUYS. Sell anything not in target,
        # and trim positions whose target is below current.
        for symbol in list(self.holdings.keys()):
            current_qty = self.holdings[symbol]
            target_qty = target_shares.get(symbol, 0)
            if target_qty < current_qty:
                excess = current_qty - target_qty
                bars = data[symbol]
                if date not in bars.index:
                    continue
                price = float(bars.loc[date]["close"])
                self.cash += excess * price
                self.holdings[symbol] -= excess
                if self.holdings[symbol] == 0:
                    del self.holdings[symbol]
                self._trade(date, symbol, "SELL", excess, price)

        # BUYS to fill toward target.
        for symbol, target_qty in target_shares.items():
            current_qty = self.holdings.get(symbol, 0)
            need = target_qty - current_qty
            if need <= 0:
                continue
            bars = data[symbol]
            if date not in bars.index:
                continue
            price = float(bars.loc[date]["close"])
            cost = need * price
            if cost > self.cash:
                # Can happen due to rounding or stale prices — buy what we can.
                need = int(self.cash // price)
                if need <= 0:
                    continue
                cost = need * price
            self.cash -= cost
            self.holdings[symbol] = current_qty + need
            self._trade(date, symbol, "BUY", need, price)

    # ------------------------------------------------------------------ #
    # Main loop                                                          #
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        print(f"\nDual Momentum backtest: {self.start} -> {self.end}")
        print(f"Risky universe: {self.risky_universe}")
        print(f"Safe asset: {self.safe_asset}")
        print(f"Lookback: {self.lookback_days} bars  |  "
              f"Top N: {self.top_n}  |  Threshold: {self.threshold:+.1%}")
        print(f"Initial capital: ${self.initial_capital:,.2f}\n")

        data = self._load_data()
        if not data:
            print("No data loaded.", file=sys.stderr)
            return

        # Filter to dates within the backtest window. Data was pre-loaded with
        # a buffer for the first lookback to be valid on day 1 of the window.
        start_ts = pd.Timestamp(self.start)
        end_ts = pd.Timestamp(self.end)
        all_dates = sorted(set().union(*(df.index for df in data.values())))
        in_window_dates = [d for d in all_dates if start_ts <= d <= end_ts]
        if not in_window_dates:
            print("No dates in window.", file=sys.stderr)
            return

        rebalances = 0
        for i, date in enumerate(in_window_dates):
            self.equity_curve.append(self._portfolio_value(date, data))
            self.equity_dates.append(date)

            if is_month_end(date, in_window_dates, i):
                returns = trailing_returns(data, date, self.lookback_days)
                target_weights = select_dual_momentum_targets(
                    returns,
                    risky_universe=self.risky_universe,
                    safe_asset=self.safe_asset,
                    top_n=self.top_n,
                    threshold=self.threshold,
                )
                self._rebalance(date, target_weights, data)
                rebalances += 1

        # Close everything at end-of-window for clean P&L
        last_date = in_window_dates[-1]
        for symbol in list(self.holdings.keys()):
            bars = data[symbol]
            if last_date not in bars.index:
                continue
            price = float(bars.loc[last_date]["close"])
            self.cash += self.holdings[symbol] * price
            self._trade(last_date, symbol, "SELL", self.holdings[symbol], price)
            self.holdings.pop(symbol)

        # Final equity (now all cash) becomes the last point on the curve
        self.equity_curve[-1] = self.cash

        self._report(data.get(self.benchmark), rebalances)

    # ------------------------------------------------------------------ #
    # Reporting                                                          #
    # ------------------------------------------------------------------ #
    def _report(self, benchmark_bars: Optional[pd.DataFrame],
                rebalances: int) -> None:
        print(f"\n{'=' * 60}")
        print("DUAL MOMENTUM BACKTEST RESULTS")
        print(f"{'=' * 60}")

        final_equity = self.equity_curve[-1] if self.equity_curve else self.cash
        total_return = (final_equity - self.initial_capital) / self.initial_capital

        print(f"Final equity:    ${final_equity:,.2f}")
        print(f"Total return:    {total_return:+.2%}")
        print(f"Rebalances:      {rebalances}")
        print(f"Total trades:    {len(self.trades)}")

        if len(self.equity_curve) > 1:
            equity_series = pd.Series(self.equity_curve, index=self.equity_dates)
            mdd = max_drawdown(equity_series)
            sharpe = annualized_sharpe(equity_series, self.risk_free_rate)
            print(f"Max drawdown:    {mdd:.2%}")
            print(f"Sharpe (ann.):   {sharpe:+.2f}")

        if self.trades:
            buys = [t for t in self.trades if t["side"] == "BUY"]
            print(f"Buys:            {len(buys)}")
            # Distribution of which symbols got bought
            from collections import Counter
            counts = Counter(t["symbol"] for t in buys)
            print("\nBuys by symbol:")
            for symbol, n in counts.most_common():
                print(f"  {symbol:5s}: {n}")

        if benchmark_bars is not None and not benchmark_bars.empty:
            in_window = benchmark_bars.loc[
                pd.Timestamp(self.start):pd.Timestamp(self.end)
            ]
            if not in_window.empty:
                first = float(in_window["close"].iloc[0])
                last = float(in_window["close"].iloc[-1])
                bench_return = (last - first) / first
                alpha = total_return - bench_return
                # Benchmark Sharpe for direct comparison
                bench_equity = (in_window["close"] / first) * self.initial_capital
                bench_sharpe = annualized_sharpe(bench_equity, self.risk_free_rate)
                bench_dd = max_drawdown(bench_equity)
                print(f"\nBenchmark ({self.benchmark}):")
                print(f"  Return:        {bench_return:+.2%}")
                print(f"  Sharpe:        {bench_sharpe:+.2f}")
                print(f"  Max drawdown:  {bench_dd:.2%}")
                print(f"\nAlpha vs benchmark:  {alpha:+.2%}")

        print(f"\n{'=' * 60}")


# --------------------------------------------------------------------------- #
# CLI entry                                                                   #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    DualMomentumBacktest().run()
