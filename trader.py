"""
trader.py — Live paper trading loop.

Wires together:
  - Alpaca API for market data + order execution
  - MomentumStrategy for signal generation
  - RiskManager for position sizing and stop-outs

Run with:  python trader.py

Always start with paper trading. Double-check that BASE_URL in config.py
points to the paper endpoint before you run this.
"""

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

try:
    from alpaca_trade_api.rest import REST, TimeFrame
except ImportError:
    print(
        "Missing dependency: pip install alpaca-trade-api",
        file=sys.stderr,
    )
    raise

import config
from monitoring import Heartbeat, setup_logging
from order_log import record_order
from risk_manager import RiskManager
from strategy import (
    MomentumStrategy,
    Signal,
    TrendStateStrategy,
    cross_sectional_momentum_filter,
    market_regime_bullish,
)


def _make_strategy():
    """Return the strategy specified in config.STRATEGY_MODE."""
    mode = getattr(config, "STRATEGY_MODE", "ema_cross")
    if mode == "trend_state":
        return TrendStateStrategy()
    return MomentumStrategy()
from trade_log import TradeLog, TradeRecord

# ---------------------------------------------------------------------------
# Logging — stdout + rotating file, plus optional webhook alerts on errors.
# ---------------------------------------------------------------------------
setup_logging(
    log_dir=getattr(config, "LOG_DIR", "logs"),
    log_file=getattr(config, "LOG_FILE", "trader.log"),
    webhook_url=os.getenv("ALERT_WEBHOOK_URL"),
)
log = logging.getLogger("trader")


# ---------------------------------------------------------------------------
# Trader
# ---------------------------------------------------------------------------
class Trader:
    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        if "YOUR_API_KEY" in config.API_KEY:
            raise RuntimeError(
                "Set ALPACA_API_KEY and ALPACA_API_SECRET in your environment "
                "(or edit config.py) before running."
            )
        self.api = REST(
            key_id=config.API_KEY,
            secret_key=config.API_SECRET,
            base_url=config.BASE_URL,
        )
        self.strategy = _make_strategy()
        self.risk = RiskManager()
        self.trade_log = TradeLog(getattr(config, "TRADE_LOG_DB", "trades.db"))
        self.heartbeat = Heartbeat(getattr(config, "HEARTBEAT_FILE",
                                           "heartbeat.json"))
        # Track entry time per position so we can log a full round-trip at exit.
        self._entry_times: dict[str, datetime] = {}
        self._sync_existing_positions()

    # ------------------------------------------------------------------ #
    # Startup                                                            #
    # ------------------------------------------------------------------ #
    def _sync_existing_positions(self) -> None:
        """Populate RiskManager state from any positions already open on Alpaca."""
        try:
            positions = self.api.list_positions()
        except Exception as exc:
            log.warning("Could not fetch existing positions: %s", exc)
            return

        for p in positions:
            if p.symbol in config.WATCHLIST:
                self.risk.register_entry(
                    symbol=p.symbol,
                    entry_price=float(p.avg_entry_price),
                    qty=float(p.qty),
                )
                # We don't know the true entry time from this call; use "now"
                # so subsequent P&L logging still has a valid timestamp.
                self._entry_times[p.symbol] = datetime.now(timezone.utc)
                log.info(
                    "Synced existing position: %s x%s @ $%.2f",
                    p.symbol, p.qty, float(p.avg_entry_price),
                )

    # ------------------------------------------------------------------ #
    # Market data                                                        #
    # ------------------------------------------------------------------ #
    def get_bars(self, symbol: str, limit: int = config.HISTORY_BARS) -> pd.DataFrame:
        """Fetch recent daily bars for a SINGLE symbol as a DataFrame."""
        try:
            bars = self.api.get_bars(
                symbol,
                TimeFrame.Day,
                limit=limit,
                feed="iex",
            ).df
        except Exception as exc:
            log.warning("Failed to fetch bars for %s: %s", symbol, exc)
            return pd.DataFrame()

        if bars.empty:
            return bars

        # Filter to this symbol if the API returned multi-symbol frame
        if "symbol" in bars.columns:
            bars = bars[bars["symbol"] == symbol]
        return bars

    BATCH_SIZE = 100  # Alpaca caps multi-symbol requests; 100 is safe.

    def get_bars_batch(
        self, symbols: list, calendar_lookback_days: int = 400,
    ) -> dict:
        """
        Batch-fetch daily bars for many symbols. Returns dict {symbol: DataFrame}.

        Uses start/end dates (not `limit`) because IEX free-tier returns only
        the most-recent bar when given a `limit` argument in batch mode.
        Default lookback (400 calendar days) covers ~252 trading days,
        sufficient for the 200-day MA and 6-month cross-sectional ranking.
        """
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=calendar_lookback_days)
        start_iso = start.isoformat()
        end_iso = end.isoformat()

        result: dict = {}
        for i in range(0, len(symbols), self.BATCH_SIZE):
            chunk = symbols[i:i + self.BATCH_SIZE]
            try:
                df = self.api.get_bars(
                    chunk,
                    TimeFrame.Day,
                    start=start_iso,
                    end=end_iso,
                    feed="iex",
                ).df
            except Exception as exc:
                log.warning("Batch bars fetch failed for chunk %d-%d: %s",
                            i, i + len(chunk), exc)
                continue
            if df.empty:
                continue
            # Drop tz so dates compare cleanly with naive Timestamps elsewhere.
            if df.index.tz is not None:
                df = df.copy()
                df.index = df.index.tz_localize(None)
            # Multi-symbol response: rows tagged with a "symbol" column.
            if "symbol" in df.columns:
                for symbol in chunk:
                    sub = df[df["symbol"] == symbol].drop(columns=["symbol"])
                    if not sub.empty:
                        result[symbol] = sub
            else:
                result[chunk[0]] = df
        return result

    def current_price(self, symbol: str) -> float:
        try:
            trade = self.api.get_latest_trade(symbol)
            return float(trade.price)
        except Exception as exc:
            log.warning("Failed to get latest price for %s: %s", symbol, exc)
            return 0.0

    # ------------------------------------------------------------------ #
    # Orders                                                             #
    # ------------------------------------------------------------------ #
    # How long to wait for a market order to fill before giving up.
    FILL_TIMEOUT_SEC = 30
    FILL_POLL_INTERVAL_SEC = 1

    def _wait_for_fill(self, order_id: str):
        """
        Poll an order until it reaches a terminal state or we time out.

        Returns the order object on a successful (partial) fill, or None
        if the order was rejected, canceled, or didn't fill in time. On
        timeout we attempt to cancel so a stale order can't fill later
        and silently desync us from broker state.
        """
        terminal = {"filled", "partially_filled", "canceled", "rejected",
                    "expired", "done_for_day"}
        deadline = time.monotonic() + self.FILL_TIMEOUT_SEC

        while time.monotonic() < deadline:
            try:
                order = self.api.get_order(order_id)
            except Exception as exc:
                log.warning("Order %s poll failed: %s", order_id, exc)
                return None

            if order.status in terminal:
                if order.status in ("filled", "partially_filled"):
                    return order
                log.warning("Order %s ended in state %s", order_id, order.status)
                return None

            time.sleep(self.FILL_POLL_INTERVAL_SEC)

        log.warning("Order %s not filled in %ds; canceling",
                    order_id, self.FILL_TIMEOUT_SEC)
        try:
            self.api.cancel_order(order_id)
        except Exception as exc:
            log.warning("Cancel of %s failed (order may still fill): %s",
                        order_id, exc)
        return None

    def submit_buy(
        self, symbol: str, notional: float, price: float,
        atr_at_entry: Optional[float] = None,
    ) -> bool:
        """
        Submit a BUY for `notional` dollars. Uses Alpaca's notional/fractional
        order API so we can size positions precisely regardless of share price.

        HARD GUARDRAIL: refuses to submit if the order would exceed available
        cash, ensuring the strategy NEVER accidentally uses margin even if
        prior sizing drifted (rounding, price moves between sizing and submit).
        """
        # Guardrail — never use margin. Skip if notional > available cash.
        if not self.dry_run:
            try:
                available_cash = float(self.api.get_account().cash)
            except Exception as exc:
                log.error("Cannot fetch cash for margin check (%s); skipping %s",
                          exc, symbol)
                return False
            if notional > available_cash:
                log.warning(
                    "Skip %s: notional $%.2f > available cash $%.2f (would use margin)",
                    symbol, notional, available_cash,
                )
                return False
        est_qty = notional / price if price > 0 else 0.0
        if self.dry_run:
            log.info("[DRY-RUN] would BUY %s ~$%.2f (~%.4f shares @ ~$%.2f)",
                     symbol, notional, est_qty, price)
            record_order(sleeve="ema", symbol=symbol, side="buy", qty=est_qty,
                         price=price, order_id="DRY-RUN", status="dry-run",
                         reason="Trend entry")
            return False  # Don't register a position we didn't take.
        try:
            order = self.api.submit_order(
                symbol=symbol,
                notional=round(notional, 2),  # Alpaca accepts to 2 decimals
                side="buy",
                type="market",          # Notional orders MUST be market type
                time_in_force="day",    # Notional orders MUST be day-tif
            )
        except Exception as exc:
            log.error("BUY submit failed for %s: %s", symbol, exc)
            record_order(sleeve="ema", symbol=symbol, side="buy", qty=est_qty,
                         price=price, status="error", reason=str(exc))
            return False

        filled = self._wait_for_fill(order.id)
        if filled is None:
            log.error("BUY %s not filled — not registering position", symbol)
            return False

        # Register using the ACTUAL fill (fractional float, e.g. 1.4234 shares).
        fill_qty = float(filled.filled_qty) if filled.filled_qty else est_qty
        fill_price = (
            float(filled.filled_avg_price)
            if filled.filled_avg_price
            else price
        )

        self.risk.register_entry(symbol, fill_price, fill_qty,
                                 atr_at_entry=atr_at_entry)
        self._entry_times[symbol] = datetime.now(timezone.utc)
        log.info("BUY  %s x%s @ $%.2f (filled)", symbol, fill_qty, fill_price)
        record_order(sleeve="ema", symbol=symbol, side="buy", qty=fill_qty,
                     price=fill_price, order_id=order.id, status="filled",
                     reason="Trend entry")
        return True

    def submit_sell(self, symbol: str, qty: float, price: float, reason: str) -> bool:
        if self.dry_run:
            log.info("[DRY-RUN] would SELL %s x%s @ ~$%.2f (%s)",
                     symbol, qty, price, reason)
            record_order(sleeve="ema", symbol=symbol, side="sell", qty=qty,
                         price=price, order_id="DRY-RUN", status="dry-run",
                         reason=reason)
            return False
        try:
            order = self.api.submit_order(
                symbol=symbol,
                qty=qty,
                side="sell",
                type=config.ORDER_TYPE,
                time_in_force=config.TIME_IN_FORCE,
            )
        except Exception as exc:
            log.error("SELL submit failed for %s: %s", symbol, exc)
            record_order(sleeve="ema", symbol=symbol, side="sell", qty=qty,
                         price=price, status="error", reason=str(exc))
            return False

        filled = self._wait_for_fill(order.id)
        if filled is None:
            # Don't deregister — we may still be holding. Better to re-check
            # next tick than to lose track of a live position.
            log.error(
                "SELL %s did not fill cleanly — leaving position registered. "
                "Investigate on Alpaca before the next tick.",
                symbol,
            )
            return False

        fill_qty = float(filled.filled_qty) if filled.filled_qty else float(qty)
        fill_price = (
            float(filled.filled_avg_price)
            if filled.filled_avg_price
            else price
        )

        # Capture entry info before we deregister so the trade log can compute P&L.
        pos = self.risk.positions.get(symbol)
        entry_price = pos.entry_price if pos else fill_price
        entry_time = self._entry_times.pop(symbol, datetime.now(timezone.utc))

        self.risk.register_exit(symbol)

        try:
            self.trade_log.record(TradeRecord(
                symbol=symbol,
                entry_time=entry_time,
                exit_time=datetime.now(timezone.utc),
                entry_price=entry_price,
                exit_price=fill_price,
                qty=fill_qty,
                pnl=(fill_price - entry_price) * fill_qty,
                reason=reason,
                source="live",
            ))
        except Exception as exc:
            # Logging failure must not break the trading loop.
            log.warning("Failed to write trade log for %s: %s", symbol, exc)

        log.info("SELL %s x%s @ $%.2f  [%s]",
                 symbol, fill_qty, fill_price, reason)
        record_order(sleeve="ema", symbol=symbol, side="sell", qty=fill_qty,
                     price=fill_price, order_id=order.id, status="filled",
                     reason=reason)
        return True

    # ------------------------------------------------------------------ #
    # Main loop                                                          #
    # ------------------------------------------------------------------ #
    def market_is_open(self) -> bool:
        try:
            return bool(self.api.get_clock().is_open)
        except Exception as exc:
            log.warning("Could not check market clock: %s", exc)
            return False

    def get_equity(self) -> float:
        return float(self.api.get_account().equity)

    def tick(self) -> None:
        """One pass through the watchlist."""
        today = datetime.now(timezone.utc).date()
        # Full account equity is reported in the heartbeat for monitoring;
        # sleeve_equity is what this strategy is actually allowed to allocate.
        # The dual_momentum sleeve uses the rest of the account independently.
        total_equity = self.get_equity()
        equity = total_equity * getattr(config, "ALLOCATION_EMA", 1.0)
        self.risk.start_day(equity, today)
        log.info("Tick: total=$%.2f sleeve=$%.2f (%.0f%%) holdings=%d",
                 total_equity, equity,
                 getattr(config, "ALLOCATION_EMA", 1.0) * 100,
                 len(self.risk.positions))

        if self.risk.check_daily_loss(equity):
            log.warning("Daily loss limit reached -- halting new entries.")

        # 1. Check stop-outs on existing positions first (always, even if halted)
        for symbol in list(self.risk.positions.keys()):
            price = self.current_price(symbol)
            if price <= 0:
                continue
            reason = self.risk.should_stop_out(symbol, price)
            if reason:
                pos = self.risk.positions[symbol]
                self.submit_sell(symbol, pos.qty, price, reason)

        # 2. Fetch bars for the whole universe in BATCH — both the per-symbol
        # signal AND the cross-sectional filter need the same data. Batching
        # collapses N per-symbol API calls into ~ceil(N/100) requests, which
        # is critical for staying under Alpaca's per-second rate cap on a
        # 200+ symbol universe.
        bars_by_symbol = self.get_bars_batch(list(config.WATCHLIST))
        log.info("Fetched bars for %d/%d symbols",
                 len(bars_by_symbol), len(config.WATCHLIST))

        # 3. Cross-sectional momentum filter — restrict entries to top quintile.
        top_momentum: Optional[dict] = None
        as_of = max(
            (df.index[-1] for df in bars_by_symbol.values() if not df.empty),
            default=None,
        )
        if getattr(config, "CROSS_SECTIONAL_ENABLED", False) and as_of is not None:
            top_momentum = cross_sectional_momentum_filter(
                bars_by_symbol, as_of,
                lookback_days=getattr(config, "CROSS_SECTIONAL_LOOKBACK_DAYS", 126),
                top_pct=getattr(config, "CROSS_SECTIONAL_TOP_PCT", 0.20),
            )
            if top_momentum is not None:
                top_list = sorted(top_momentum)
                log.info("Cross-sectional top %d (eligible for entry): %s",
                         len(top_list), ", ".join(top_list))
            else:
                log.info("Cross-sectional filter: insufficient data, inactive this tick")

        # 3a. Drop-out exit -- close positions no longer in the cross-sectional
        # top-N. Symmetric to entry logic. Only fires when filter has data.
        if (
            getattr(config, "EXIT_ON_MOMENTUM_DROP", False)
            and top_momentum is not None
        ):
            for symbol in list(self.risk.positions.keys()):
                if symbol in top_momentum:
                    continue
                bars = bars_by_symbol.get(symbol)
                if bars is None or bars.empty:
                    continue
                price = float(bars.iloc[-1]["close"])
                pos = self.risk.positions[symbol]
                self.submit_sell(
                    symbol, pos.qty, price,
                    f"Dropped out of top {getattr(config, 'CROSS_SECTIONAL_TOP_PCT', 0.20):.0%} momentum",
                )

        # 3b. Market regime filter — only enter longs when SPY > 200-day SMA.
        regime_allows_entries = True
        if getattr(config, "MARKET_REGIME_ENABLED", False) and as_of is not None:
            regime_symbol = getattr(config, "MARKET_REGIME_SYMBOL", "SPY")
            ma_days = getattr(config, "MARKET_REGIME_MA_DAYS", 200)
            # SPY isn't in the watchlist — fetch separately. limit gives us
            # enough history for the SMA computation.
            spy_bars = self.get_bars(regime_symbol, limit=ma_days + 20)
            if not spy_bars.empty:
                bullish = market_regime_bullish(spy_bars, spy_bars.index[-1], ma_days)
                if bullish is False:
                    regime_allows_entries = False
                    log.info("Market regime BEARISH (%s below %d-day SMA) — "
                             "skipping all new entries this tick.",
                             regime_symbol, ma_days)

        # 4. Evaluate signals
        n_buys = n_sells = n_holds = n_skipped_no_top = n_skipped_other = 0
        eligible_buys = []  # (symbol, signal_result) for buys that passed all filters

        for symbol, bars in bars_by_symbol.items():
            holding = symbol in self.risk.positions
            result = self.strategy.generate_signal(symbol, bars, holding=holding)
            if result is None:
                continue

            if result.signal == Signal.BUY:
                n_buys += 1
                if not self.risk.can_open_position(symbol):
                    n_skipped_other += 1
                    continue
                if not regime_allows_entries:
                    n_skipped_other += 1
                    continue
                if top_momentum is not None and symbol not in top_momentum:
                    n_skipped_no_top += 1
                    continue
                eligible_buys.append((symbol, result))

            elif result.signal == Signal.SELL and holding:
                n_sells += 1
                pos = self.risk.positions[symbol]
                self.submit_sell(symbol, pos.qty, result.price, result.reason)
            else:
                n_holds += 1

        log.info(
            "Signals: %d buy, %d sell, %d hold | BUYs filtered: %d not-top-N, %d other "
            "| BUYs eligible after filters: %d",
            n_buys, n_sells, n_holds, n_skipped_no_top, n_skipped_other,
            len(eligible_buys),
        )

        # Rank eligible buys by momentum score (strongest first) so the position
        # cap keeps the highest-conviction signals, not the alphabetically first.
        # When cross-sectional is active, reuse its already-computed scores.
        # When it's off, compute a trailing return from the same bar data.
        _cs_lookback = getattr(config, "CROSS_SECTIONAL_LOOKBACK_DAYS", 126)

        def _momentum_score(sym: str) -> float:
            if top_momentum is not None:
                return top_momentum.get(sym, -float("inf"))
            bars = bars_by_symbol.get(sym)
            if bars is None or len(bars) < _cs_lookback + 1:
                return -float("inf")
            past = float(bars.iloc[-_cs_lookback - 1]["close"])
            curr = float(bars.iloc[-1]["close"])
            return (curr - past) / past if past > 0 else -float("inf")

        eligible_buys.sort(key=lambda item: _momentum_score(item[0]), reverse=True)

        # Submit eligible buys. Re-check the cap each iteration so it's
        # honored as positions accumulate within this tick. In dry-run we
        # simulate the cap with `would_open` since no real positions register.
        max_positions = self.risk.max_open_positions
        would_open = 0
        for symbol, result in eligible_buys:
            simulated_count = len(self.risk.positions) + would_open
            if self.dry_run:
                if simulated_count >= max_positions:
                    log.info("Would-cap at %d positions; %d further BUYs skipped.",
                             max_positions, len(eligible_buys) - would_open)
                    break
            elif not self.risk.can_open_position(symbol):
                log.info("Cap reached at %d positions; remaining BUYs skipped.",
                         len(self.risk.positions))
                break
            stop = self.risk.stop_price_for(result.price, result.atr)
            notional = self.risk.calculate_position_size(equity, result.price, stop)
            # Skip if budget is below Alpaca's $1 fractional minimum.
            if notional < 1.0:
                log.info("Skip %s: notional $%.2f below minimum", symbol, notional)
                continue
            ok = self.submit_buy(symbol, notional, result.price,
                                 atr_at_entry=result.atr)
            if self.dry_run:
                would_open += 1  # Count regardless of `ok`; dry-run never returns True

        # 3. Write heartbeat so external monitors can see we're alive and
        #    know what the bot is holding. Failure here must not kill a tick.
        try:
            self.heartbeat.write(
                status="ok",
                equity=total_equity,
                sleeve_equity=equity,
                trading_halted=self.risk.trading_halted,
                open_positions={
                    sym: {"qty": pos.qty, "entry_price": pos.entry_price}
                    for sym, pos in self.risk.positions.items()
                },
                watchlist=list(config.WATCHLIST),
            )
        except Exception as exc:
            log.warning("Heartbeat write failed: %s", exc)

    def run(self, once: bool = False, ignore_market_hours: bool = False) -> None:
        mode = (
            "ONE-SHOT [DRY-RUN]" if once and self.dry_run
            else "ONE-SHOT" if once
            else "[DRY-RUN]" if self.dry_run
            else "LIVE"
        )
        log.info("Trader starting (%s). Endpoint: %s", mode, config.BASE_URL)
        log.info("Strategy: %s, Universe: %d symbols",
                 getattr(config, "STRATEGY_MODE", "ema_cross"),
                 len(config.WATCHLIST))

        if once:
            # Single tick for inspection / preview. Skip market-hours check
            # in this mode so you can preview anytime.
            try:
                self.tick()
            except Exception as exc:
                log.exception("Error during one-shot tick: %s", exc)
            return

        _stop_file = os.path.join(getattr(config, "LOG_DIR", "logs"), "STOP")
        try:
            while True:
                if os.path.exists(_stop_file):
                    try:
                        os.remove(_stop_file)
                    except OSError:
                        pass
                    log.info("Stop file detected (%s) — shutting down cleanly.", _stop_file)
                    break
                try:
                    if ignore_market_hours or self.market_is_open():
                        self.tick()
                    else:
                        log.info("Market closed. Sleeping.")
                except Exception as exc:
                    # Catch business-logic / API errors so one bad tick doesn't
                    # kill the loop, but let KeyboardInterrupt propagate so
                    # Ctrl+C exits immediately instead of sleeping another cycle.
                    log.exception("Unexpected error in main loop: %s", exc)

                time.sleep(config.POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            log.info("Interrupted by user. Shutting down.")


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Run the EMA / trend-state trader.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run normally but do NOT submit any orders.")
    parser.add_argument("--preview", action="store_true",
                        help="One-shot: run a single tick and exit. Implies --dry-run "
                             "and skips market-hours check. Useful for inspecting "
                             "what the strategy would do right now.")
    args = parser.parse_args()

    dry_run = args.dry_run or args.preview
    once = args.preview
    ignore_hours = args.preview

    Trader(dry_run=dry_run).run(once=once, ignore_market_hours=ignore_hours)
    return 0


if __name__ == "__main__":
    sys.exit(main())
