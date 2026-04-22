"""
backtest.py — Historical simulation of the momentum strategy.

Fetches daily bars through the configured data source (yfinance by default),
walks them forward one bar at a time, and runs the same strategy + risk
logic used by the live trader. Reports P&L, win rate, drawdown, Sharpe,
and a buy-and-hold benchmark comparison.

Run with:  python backtest.py
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import config
from data_source import get_data_source
from risk_manager import RiskManager
from strategy import (
    MomentumStrategy,
    Signal,
    TrendStateStrategy,
    cross_sectional_momentum_filter,
    market_regime_bullish,
)


def _make_strategy():
    """Return strategy instance per config.STRATEGY_MODE."""
    mode = getattr(config, "STRATEGY_MODE", "ema_cross")
    if mode == "trend_state":
        return TrendStateStrategy()
    return MomentumStrategy()
from trade_log import TradeLog, TradeRecord


@dataclass
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    reason: str


@dataclass
class BacktestEngine:
    start: str = config.BACKTEST_START
    end: str = config.BACKTEST_END
    initial_capital: float = config.BACKTEST_INITIAL_CAPITAL
    commission: float = config.BACKTEST_COMMISSION

    # Strategy chosen per config.STRATEGY_MODE; see _make_strategy() above.
    strategy: object = field(default_factory=_make_strategy)
    risk: RiskManager = field(default_factory=RiskManager)

    # Optional persistent log. If None, trades are kept only in self.trades.
    trade_log: Optional[TradeLog] = None
    run_id: Optional[str] = None

    cash: float = 0.0
    equity_curve: List[float] = field(default_factory=list)
    equity_dates: List[pd.Timestamp] = field(default_factory=list)
    trades: List[Trade] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cash = self.initial_capital

    # ------------------------------------------------------------------ #
    # Data loading                                                       #
    # ------------------------------------------------------------------ #
    data_source_name: str = config.BACKTEST_DATA_SOURCE
    benchmark_symbol: str = config.BENCHMARK_SYMBOL
    risk_free_rate: float = config.RISK_FREE_RATE

    def load_data(self) -> Dict[str, pd.DataFrame]:
        source = get_data_source(self.data_source_name)
        # Pull benchmark along with the watchlist so we only hit the
        # data source once per backtest.
        symbols = list(config.WATCHLIST) + [self.benchmark_symbol]
        data = source.load(symbols=symbols, start=self.start, end=self.end)
        return data

    # ------------------------------------------------------------------ #
    # Simulation                                                         #
    # ------------------------------------------------------------------ #
    def portfolio_equity(self, date: pd.Timestamp, data: Dict[str, pd.DataFrame]) -> float:
        """Cash + mark-to-market value of open positions."""
        value = self.cash
        for symbol, pos in self.risk.positions.items():
            bars = data[symbol]
            if date in bars.index:
                value += pos.qty * float(bars.loc[date]["close"])
            else:
                value += pos.qty * pos.entry_price  # Fallback
        return value

    def _close_position(
        self,
        symbol: str,
        date: pd.Timestamp,
        price: float,
        reason: str,
    ) -> None:
        pos = self.risk.positions[symbol]
        proceeds = pos.qty * price - self.commission
        cost_basis = pos.qty * pos.entry_price
        pnl = proceeds - cost_basis
        self.cash += proceeds

        # Find corresponding entry date from the most recent open trade
        entry_date = getattr(pos, "_entry_date", date)
        trade = Trade(
            symbol=symbol,
            entry_date=entry_date,
            exit_date=date,
            entry_price=pos.entry_price,
            exit_price=price,
            qty=float(pos.qty),
            pnl=pnl,
            reason=reason,
        )
        self.trades.append(trade)

        if self.trade_log is not None:
            # Write to SQLite so backtest and live runs share the same history.
            try:
                self.trade_log.record(TradeRecord(
                    symbol=symbol,
                    entry_time=pd.Timestamp(entry_date).to_pydatetime(),
                    exit_time=pd.Timestamp(date).to_pydatetime(),
                    entry_price=pos.entry_price,
                    exit_price=price,
                    qty=float(pos.qty),
                    pnl=pnl,
                    reason=reason,
                    source="backtest",
                    run_id=self.run_id,
                ))
            except Exception as exc:
                print(f"Warning: trade_log write failed for {symbol}: {exc}")

        self.risk.register_exit(symbol)

    def _open_position(
        self, symbol: str, date: pd.Timestamp, price: float, qty: float,
        atr_at_entry: Optional[float] = None,
    ) -> None:
        # qty is now fractional float (matches live fractional-share behavior).
        cost = qty * price + self.commission
        self.cash -= cost
        self.risk.register_entry(symbol, price, qty, atr_at_entry=atr_at_entry)
        # Stash entry date on the position for trade reporting
        self.risk.positions[symbol]._entry_date = date  # type: ignore[attr-defined]

    def run(self) -> None:
        print(f"\nBacktesting {self.start} -> {self.end}")
        print(f"Data source: {self.data_source_name}")
        print(f"Initial capital: ${self.initial_capital:,.2f}\n")

        data = self.load_data()
        if not data:
            print("No data loaded. Check your dates and data source.")
            return

        # Separate benchmark from tradable universe — we fetch it so the
        # report can compare, but the strategy should not trade it.
        benchmark_bars = data.pop(self.benchmark_symbol, None)
        tradable = {s: df for s, df in data.items() if s in config.WATCHLIST}

        if not tradable:
            print("No tradable symbols loaded.")
            return

        # Pre-compute indicators ONCE per symbol on the full history. The
        # strategy's compute_indicators is idempotent — when generate_signal
        # later calls it on a slice that already contains the columns, it
        # returns immediately. Without this step, we'd re-roll the moving
        # averages on a growing slice every day for every symbol = O(N^2).
        print("Precomputing indicators for all symbols...")
        tradable = {
            s: self.strategy.compute_indicators(df)
            for s, df in tradable.items()
        }

        # Build unified date index across all tradable symbols
        all_dates = sorted(set().union(*(df.index for df in tradable.values())))

        cs_enabled = getattr(config, "CROSS_SECTIONAL_ENABLED", False)
        cs_lookback = getattr(config, "CROSS_SECTIONAL_LOOKBACK_DAYS", 126)
        cs_top_pct = getattr(config, "CROSS_SECTIONAL_TOP_PCT", 0.20)
        mr_enabled = getattr(config, "MARKET_REGIME_ENABLED", False)
        mr_ma_days = getattr(config, "MARKET_REGIME_MA_DAYS", 200)

        for date in all_dates:
            equity = self.portfolio_equity(date, tradable)
            self.equity_curve.append(equity)
            self.equity_dates.append(date)
            self.risk.start_day(
                equity,
                date.date() if hasattr(date, "date") else date,
            )

            # 1. Exit checks on open positions
            for symbol in list(self.risk.positions.keys()):
                bars = tradable[symbol]
                if date not in bars.index:
                    continue
                price = float(bars.loc[date]["close"])
                reason = self.risk.should_stop_out(symbol, price)
                if reason:
                    self._close_position(symbol, date, price, reason)

            # Cross-sectional momentum: rank universe by trailing return and
            # restrict new entries to the top quintile. Returns None during
            # warmup (insufficient history) — treat as filter inactive.
            top_momentum: Optional[set] = None
            if cs_enabled:
                top_momentum = cross_sectional_momentum_filter(
                    tradable, date,
                    lookback_days=cs_lookback,
                    top_pct=cs_top_pct,
                )

            # Drop-out exit: close positions that fell out of the top-N by
            # trailing momentum. Symmetric to entry logic — if a stock no longer
            # qualifies as "top momentum," don't keep holding it through the
            # noise until a stop fires. Only applies when the cross-sectional
            # filter has enough data to rank.
            if (
                getattr(config, "EXIT_ON_MOMENTUM_DROP", False)
                and top_momentum is not None
            ):
                for symbol in list(self.risk.positions.keys()):
                    if symbol in top_momentum:
                        continue
                    bars = tradable.get(symbol)
                    if bars is None or date not in bars.index:
                        continue
                    price = float(bars.loc[date]["close"])
                    self._close_position(
                        symbol, date, price,
                        f"Dropped out of top {cs_top_pct:.0%} by momentum",
                    )

            # Market regime: only allow entries when SPY > its 200-day SMA.
            # None during warmup → filter inactive (allow entries normally).
            regime_allows_entries = True
            if mr_enabled and benchmark_bars is not None:
                bullish = market_regime_bullish(benchmark_bars, date, mr_ma_days)
                if bullish is False:
                    regime_allows_entries = False

            # 2. Signal evaluation
            for symbol, bars in tradable.items():
                if date not in bars.index:
                    continue
                history = bars.loc[:date]
                holding = symbol in self.risk.positions
                result = self.strategy.generate_signal(symbol, history, holding=holding)
                if result is None:
                    continue

                if result.signal == Signal.BUY and self.risk.can_open_position(symbol):
                    if not regime_allows_entries:
                        continue  # Market regime is bearish — no new longs
                    if top_momentum is not None and symbol not in top_momentum:
                        continue  # Bottom 80% by trailing return — skip entry
                    stop = self.risk.stop_price_for(result.price, result.atr)
                    notional = self.risk.calculate_position_size(
                        equity, result.price, stop)
                    # Cap notional by available cash; convert to fractional shares.
                    notional = min(notional, self.cash - self.commission)
                    qty = notional / result.price if result.price > 0 else 0.0
                    if qty > 0 and notional >= 1.0:
                        self._open_position(
                            symbol, date, result.price, qty,
                            atr_at_entry=result.atr,
                        )

                elif result.signal == Signal.SELL and holding:
                    self._close_position(symbol, date, result.price, result.reason)

        # Close any positions still open at the end at the final bar's price
        for symbol in list(self.risk.positions.keys()):
            bars = tradable[symbol]
            last_date = bars.index[-1]
            last_price = float(bars.iloc[-1]["close"])
            self._close_position(symbol, last_date, last_price, "End of backtest")

        self.report(benchmark_bars=benchmark_bars)

    # ------------------------------------------------------------------ #
    # Reporting                                                          #
    # ------------------------------------------------------------------ #
    def report(self, benchmark_bars: Optional[pd.DataFrame] = None) -> None:
        print(f"\n{'=' * 60}")
        print("BACKTEST RESULTS")
        print(f"{'=' * 60}")

        final_equity = self.equity_curve[-1] if self.equity_curve else self.cash
        total_return = (final_equity - self.initial_capital) / self.initial_capital

        print(f"Final equity:   ${final_equity:,.2f}")
        print(f"Total return:   {total_return:+.2%}")
        print(f"Total trades:   {len(self.trades)}")

        # Risk-adjusted metrics from the daily equity curve
        if len(self.equity_curve) > 1:
            equity_series = pd.Series(self.equity_curve, index=self.equity_dates)
            mdd = max_drawdown(equity_series)
            sharpe = annualized_sharpe(equity_series, self.risk_free_rate)
            print(f"Max drawdown:   {mdd:.2%}")
            print(f"Sharpe (ann.):  {sharpe:+.2f}")

        if self.trades:
            trades_df = pd.DataFrame([t.__dict__ for t in self.trades])
            wins = trades_df[trades_df["pnl"] > 0]
            losses = trades_df[trades_df["pnl"] <= 0]

            win_rate = len(wins) / len(trades_df)
            avg_win = wins["pnl"].mean() if len(wins) else 0.0
            avg_loss = losses["pnl"].mean() if len(losses) else 0.0
            total_pnl = trades_df["pnl"].sum()

            print(f"Win rate:       {win_rate:.1%}")
            print(f"Avg win:        ${avg_win:+,.2f}")
            print(f"Avg loss:       ${avg_loss:+,.2f}")
            print(f"Net P&L:        ${total_pnl:+,.2f}")

            print("\nPer-symbol breakdown:")
            for symbol in trades_df["symbol"].unique():
                sym_trades = trades_df[trades_df["symbol"] == symbol]
                sym_pnl = sym_trades["pnl"].sum()
                sym_wins = len(sym_trades[sym_trades["pnl"] > 0])
                print(
                    f"  {symbol:5s}: {len(sym_trades):2d} trades, "
                    f"{sym_wins}/{len(sym_trades)} wins, "
                    f"P&L: ${sym_pnl:+,.2f}"
                )
        else:
            print("No trades executed.")

        # Benchmark comparison: what would a simple buy-and-hold of the
        # benchmark symbol have returned over the same window?
        if benchmark_bars is not None and not benchmark_bars.empty:
            first = float(benchmark_bars["close"].iloc[0])
            last = float(benchmark_bars["close"].iloc[-1])
            bench_return = (last - first) / first
            alpha = total_return - bench_return
            print(f"\nBenchmark ({self.benchmark_symbol}): {bench_return:+.2%}")
            print(f"Alpha vs benchmark:    {alpha:+.2%}")

        print(f"\n{'=' * 60}")


# --------------------------------------------------------------------------- #
# Metric helpers — pulled out of BacktestEngine so they're easy to unit-test. #
# --------------------------------------------------------------------------- #
def max_drawdown(equity: pd.Series) -> float:
    """
    Return the worst peak-to-trough drawdown of an equity curve, as a
    positive decimal (0.25 == 25% drawdown). An always-increasing curve
    returns 0.0.
    """
    if len(equity) == 0:
        return 0.0
    running_peak = equity.cummax()
    drawdown = (equity - running_peak) / running_peak
    return float(-drawdown.min())


def annualized_sharpe(equity: pd.Series, risk_free: float = 0.0,
                      periods_per_year: int = 252) -> float:
    """
    Annualized Sharpe ratio from an equity curve.

    Uses daily arithmetic returns, subtracts the per-period risk-free rate,
    and scales by sqrt(periods_per_year). Returns 0.0 if the curve is too
    short or has zero variance.
    """
    if len(equity) < 2:
        return 0.0
    returns = equity.pct_change().dropna()
    if returns.empty or returns.std() == 0 or math.isnan(returns.std()):
        return 0.0
    excess = returns - (risk_free / periods_per_year)
    return float((excess.mean() / returns.std()) * np.sqrt(periods_per_year))


if __name__ == "__main__":
    # Default CLI writes trades to the shared SQLite log with a timestamped
    # run_id so successive runs don't get mixed together.
    from datetime import datetime as _dt

    run_id = _dt.utcnow().strftime("bt-%Y%m%d-%H%M%S")
    engine = BacktestEngine(
        trade_log=TradeLog(config.TRADE_LOG_DB),
        run_id=run_id,
    )
    engine.run()
