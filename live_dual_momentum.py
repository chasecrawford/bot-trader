"""
live_dual_momentum.py — Run GEM dual momentum on Alpaca paper trading.

Designed to be run ONCE PER DAY during market hours. The script:
  1. Verifies it's pointed at the paper endpoint (refuses to run on live)
  2. Checks Alpaca's calendar to see if today is the last trading day of the month
  3. If NOT month-end: logs heartbeat and exits (no trades)
  4. If IS month-end: fetches SPY/EFA/AGG history, computes target weights via the
     same dual_momentum.select_dual_momentum_targets we backtested, and submits
     market orders to rebalance from current holdings to target.

Run manually:
    python live_dual_momentum.py

Cron (Windows Task Scheduler / Linux cron) — daily at 3:00 PM ET, weekdays:
    0 15 * * 1-5 cd /path/to/bot-trader && python live_dual_momentum.py

Force a rebalance for testing (bypass month-end check):
    python live_dual_momentum.py --force

This module deliberately does NOT import from trader.py or strategy.py — those
implement the EMA-cross strategy that we validated as not having edge.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import pandas as pd

try:
    from alpaca_trade_api.rest import REST, TimeFrame
except ImportError:
    print("Missing dependency: pip install alpaca-trade-api", file=sys.stderr)
    raise

import config
from dual_momentum import select_dual_momentum_targets, trailing_returns
from monitoring import setup_logging
from order_log import record_order


setup_logging(
    log_dir=getattr(config, "LOG_DIR", "logs"),
    log_file="live_dual_momentum.log",
)
log = logging.getLogger("dual_momentum")


# Time to wait for a single order to fill before giving up. Market orders on
# liquid ETFs typically fill quickly, but Alpaca paper has been observed taking
# tens of seconds — give it a comfortable margin.
FILL_TIMEOUT_SEC = 120
FILL_POLL_SEC = 2


class GEMLiveRunner:
    """Monthly GEM rebalance runner."""

    def __init__(self, dry_run: bool = False) -> None:
        if "YOUR_API_KEY" in config.API_KEY:
            raise RuntimeError(
                "Set ALPACA_API_KEY and ALPACA_API_SECRET in .env before running."
            )
        # Hard guardrail — never let this script touch live money accidentally.
        if "paper" not in config.BASE_URL:
            raise RuntimeError(
                f"Refusing to run against non-paper endpoint: {config.BASE_URL}\n"
                f"This script only operates on paper accounts."
            )

        self.api = REST(
            key_id=config.API_KEY,
            secret_key=config.API_SECRET,
            base_url=config.BASE_URL,
        )
        self.dry_run = dry_run
        self.risky_universe: List[str] = list(config.DUAL_MOMENTUM_RISKY)
        self.safe_asset: str = config.DUAL_MOMENTUM_SAFE
        self.universe: List[str] = self.risky_universe + [self.safe_asset]
        self.lookback: int = config.DUAL_MOMENTUM_LOOKBACK_DAYS
        self.top_n: int = config.DUAL_MOMENTUM_TOP_N
        self.threshold: float = config.DUAL_MOMENTUM_THRESHOLD

    # ------------------------------------------------------------------ #
    # Calendar — Alpaca is the source of truth for trading days          #
    # ------------------------------------------------------------------ #
    def is_today_month_end(self) -> bool:
        """True if today is open AND the next trading day is in a different month."""
        today = datetime.now(timezone.utc).date()
        end = (today + timedelta(days=14)).isoformat()
        try:
            calendar = self.api.get_calendar(start=today.isoformat(), end=end)
        except Exception as exc:
            log.error("Failed to fetch market calendar: %s", exc)
            return False

        if not calendar:
            log.info("No upcoming trading days returned by Alpaca.")
            return False

        first_open = self._calendar_date(calendar[0])
        if first_open != today:
            log.info("Today (%s) is not a trading day; market opens next on %s.",
                     today, first_open)
            return False

        if len(calendar) < 2:
            # Today is open and there's no future day in the window — assume
            # month-end conservatively (extremely rare edge case).
            return True

        next_trading = self._calendar_date(calendar[1])
        return today.month != next_trading.month

    @staticmethod
    def _calendar_date(entry):
        """Alpaca returns calendar dates that may be datetime, date, or string."""
        d = entry.date
        if hasattr(d, "date"):  # datetime → date
            return d.date()
        if isinstance(d, str):
            return datetime.fromisoformat(d).date()
        return d  # already a date

    # ------------------------------------------------------------------ #
    # Data fetching                                                      #
    # ------------------------------------------------------------------ #
    def fetch_bars(self, symbol: str, lookback_calendar_days: int) -> pd.DataFrame:
        """
        Fetch daily bars going back `lookback_calendar_days` calendar days.
        Using start/end dates is more reliable than `limit` on Alpaca's free
        tier, which has been observed silently capping bar counts.
        """
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=lookback_calendar_days)
        try:
            # feed="iex" is required on free-tier Alpaca paper accounts;
            # SIP requires a paid subscription. IEX data is sufficient for
            # daily-bar momentum on liquid ETFs.
            bars = self.api.get_bars(
                symbol,
                TimeFrame.Day,
                start=start.isoformat(),
                end=end.isoformat(),
                feed="iex",
            ).df
        except Exception as exc:
            log.warning("Failed to fetch bars for %s: %s", symbol, exc)
            return pd.DataFrame()
        if bars.empty:
            return bars
        if "symbol" in bars.columns:
            bars = bars[bars["symbol"] == symbol]
        # Drop tz so dates align with our `as_of` Timestamp comparisons
        if bars.index.tz is not None:
            bars.index = bars.index.tz_localize(None)
        return bars

    # ------------------------------------------------------------------ #
    # Account / positions                                                #
    # ------------------------------------------------------------------ #
    def get_equity(self) -> float:
        return float(self.api.get_account().equity)

    def get_current_holdings(self) -> Dict[str, float]:
        """Return {symbol: qty (fractional float)} for all current positions."""
        try:
            positions = self.api.list_positions()
        except Exception as exc:
            log.error("Failed to list positions: %s", exc)
            return {}
        return {p.symbol: float(p.qty) for p in positions}

    # ------------------------------------------------------------------ #
    # Order submission with fill-wait                                    #
    # ------------------------------------------------------------------ #
    def _wait_for_fill(self, order_id: str) -> bool:
        deadline = time.time() + FILL_TIMEOUT_SEC
        last_status = None
        while time.time() < deadline:
            try:
                order = self.api.get_order(order_id)
            except Exception as exc:
                log.warning("Couldn't poll order %s: %s", order_id, exc)
                time.sleep(FILL_POLL_SEC)
                continue
            if order.status != last_status:
                log.info("  Order %s status: %s (filled %s/%s)",
                         order_id[:8], order.status,
                         order.filled_qty, order.qty)
                last_status = order.status
            if order.status == "filled":
                return True
            if order.status in ("canceled", "rejected", "expired"):
                log.warning("Order %s ended in status %s", order_id, order.status)
                return False
            time.sleep(FILL_POLL_SEC)

        log.warning("Order %s did not fill within %ds; canceling.",
                    order_id, FILL_TIMEOUT_SEC)
        try:
            self.api.cancel_order(order_id)
        except Exception:
            pass
        return False

    def submit_and_wait(self, symbol: str, qty_or_notional: float, side: str,
                        price_hint: float = 0.0) -> bool:
        """
        Submit a market order and block until filled (or timeout).

        For BUYS: `qty_or_notional` is interpreted as a NOTIONAL dollar amount
        (Alpaca handles the fractional share calc).
        For SELLS: `qty_or_notional` is interpreted as a fractional share QTY
        (Alpaca lets you sell fractional positions; you can't sell "$X" because
        you might not own enough notional, so qty is the safer expression).

        In dry_run mode, logs the would-be order and returns True without
        touching Alpaca.
        """
        side = side.lower()
        # For order-log + display purposes, normalize to a "qty-ish" number
        log_qty = (qty_or_notional / price_hint
                   if side == "buy" and price_hint > 0
                   else qty_or_notional)

        if self.dry_run:
            verb = ("BUY ~$%.2f (~%.4f shares)" % (qty_or_notional, log_qty)
                    if side == "buy"
                    else "SELL %.4f shares" % qty_or_notional)
            log.info("[DRY-RUN] would %s %s", verb, symbol)
            record_order(sleeve="dm", symbol=symbol, side=side, qty=log_qty,
                         price=price_hint, order_id="DRY-RUN",
                         status="dry-run", reason="Monthly rebalance")
            return True

        try:
            kwargs = {"side": side, "type": "market", "time_in_force": "day",
                      "symbol": symbol}
            if side == "buy":
                kwargs["notional"] = round(qty_or_notional, 2)
            else:
                kwargs["qty"] = round(qty_or_notional, 4)
            order = self.api.submit_order(**kwargs)
        except Exception as exc:
            log.error("%s submit failed for %s: %s", side.upper(), symbol, exc)
            record_order(sleeve="dm", symbol=symbol, side=side, qty=log_qty,
                         price=price_hint, status="error", reason=str(exc))
            return False
        log.info("%s %s submitted (order %s); waiting for fill...",
                 side.upper(), symbol, order.id)
        record_order(sleeve="dm", symbol=symbol, side=side, qty=log_qty,
                     price=price_hint, order_id=order.id,
                     status="submitted", reason="Monthly rebalance")
        filled = self._wait_for_fill(order.id)
        if filled:
            record_order(sleeve="dm", symbol=symbol, side=side, qty=log_qty,
                         price=price_hint, order_id=order.id,
                         status="filled", reason="Monthly rebalance")
        return filled

    # ------------------------------------------------------------------ #
    # Rebalance                                                          #
    # ------------------------------------------------------------------ #
    def rebalance(self) -> None:
        log.info("=" * 60)
        log.info("MONTH-END REBALANCE")
        log.info("=" * 60)

        # 0. Refuse to operate when the market is closed (real runs only).
        # Day-time-in-force orders won't execute outside hours. In dry_run /
        # preview mode we skip this so you can inspect strategy state any time.
        if not self.dry_run:
            try:
                clock = self.api.get_clock()
            except Exception as exc:
                log.error("Cannot fetch market clock: %s", exc)
                return
            if not clock.is_open:
                log.warning(
                    "Market is CLOSED (current %s, next open %s). "
                    "Rebalance aborted; re-run during market hours.",
                    clock.timestamp, clock.next_open,
                )
                return

        # 1. Pull bars for entire universe. Calendar-day lookback covers the
        # 252 trading days needed (with comfortable margin for weekends/holidays).
        calendar_lookback = int(self.lookback * 1.6) + 30  # ~440 days for 252 trading
        bars_by_symbol: Dict[str, pd.DataFrame] = {}
        for symbol in self.universe:
            bars = self.fetch_bars(symbol, calendar_lookback)
            if bars.empty:
                log.warning("No bars for %s; proceeding without it.", symbol)
                continue
            log.info("Loaded %s: %d bars from %s to %s",
                     symbol, len(bars), bars.index[0].date(), bars.index[-1].date())
            bars_by_symbol[symbol] = bars

        if not bars_by_symbol:
            log.error("No bar data available for any universe symbol; aborting.")
            return

        # Verify we have enough history to rank — abort instead of silently
        # falling through to "all safe" when the data is insufficient.
        rankable = [s for s in self.risky_universe
                    if s in bars_by_symbol and len(bars_by_symbol[s]) >= self.lookback + 1]
        if not rankable:
            log.error("Not enough history to rank any risky asset (need %d bars). "
                      "Aborting rather than blindly going to safe asset.",
                      self.lookback + 1)
            return

        # 2. Compute trailing returns and target weights.
        as_of = max(df.index[-1] for df in bars_by_symbol.values())
        returns = trailing_returns(bars_by_symbol, as_of, self.lookback)
        log.info("Trailing %d-day returns (as of %s):",
                 self.lookback, as_of.date())
        for sym, ret in sorted(returns.items(), key=lambda kv: kv[1], reverse=True):
            log.info("  %-5s  %+7.2f%%", sym, ret * 100)

        target_weights = select_dual_momentum_targets(
            returns,
            risky_universe=self.risky_universe,
            safe_asset=self.safe_asset,
            top_n=self.top_n,
            threshold=self.threshold,
        )
        log.info("Target weights: %s",
                 {s: f"{w:.0%}" for s, w in target_weights.items()})

        # 3. Snapshot current account state. Only DM-universe positions are
        # this strategy's responsibility — EMA-cross may hold individual stocks
        # in the same account, and we must NOT touch those.
        total_equity = self.get_equity()
        sleeve_equity = total_equity * getattr(config, "ALLOCATION_DM", 1.0)
        all_holdings = self.get_current_holdings()
        # holdings are FRACTIONAL floats now (e.g. 1.4234 shares)
        dm_holdings = {s: q for s, q in all_holdings.items() if s in self.universe}
        log.info("Total account equity:  $%.2f", total_equity)
        log.info("DM sleeve equity:      $%.2f (%.0f%% of total)",
                 sleeve_equity, getattr(config, "ALLOCATION_DM", 1.0) * 100)
        log.info("DM holdings:           %s",
                 {s: f"{q:.4f}" for s, q in dm_holdings.items()}
                 if dm_holdings else "(none)")
        if all_holdings != dm_holdings:
            other = {s: q for s, q in all_holdings.items() if s not in self.universe}
            log.info("Other-strategy holdings (untouched): %s", other)

        # Helper -- last close price for each symbol
        def last_price(sym: str) -> float:
            bars = bars_by_symbol.get(sym)
            if bars is None or bars.empty:
                return 0.0
            return float(bars.iloc[-1]["close"])

        # 4. Compute target NOTIONAL per symbol (dollar amount) and the
        # fractional share equivalent for delta calculations.
        target_notional: Dict[str, float] = {}
        target_qty: Dict[str, float] = {}
        for symbol, weight in target_weights.items():
            price = last_price(symbol)
            if price <= 0:
                log.warning("Cannot allocate %s; no price data.", symbol)
                continue
            target_notional[symbol] = sleeve_equity * weight
            target_qty[symbol] = target_notional[symbol] / price
            log.info("Target: %-5s = $%.2f (%.4f shares @ $%.2f, %.0f%% sleeve)",
                     symbol, target_notional[symbol], target_qty[symbol],
                     price, weight * 100)

        # 5. SELLS first (frees cash for BUYS). Iterate DM_HOLDINGS only --
        # never touch positions outside the DM universe.
        for symbol, current_qty in dm_holdings.items():
            tgt_qty = target_qty.get(symbol, 0.0)
            if current_qty > tgt_qty:
                excess_qty = current_qty - tgt_qty
                # Round qty to 4 decimals for Alpaca (it accepts up to 4 dp).
                self.submit_and_wait(
                    symbol, round(excess_qty, 4), "sell",
                    price_hint=last_price(symbol),
                )

        # 6. BUYS to fill toward targets. Use NOTIONAL so Alpaca handles the
        # fractional share calc itself (avoids rounding drift from price drift
        # between when we computed and when the order fills).
        for symbol, tgt_notional in target_notional.items():
            current_qty = dm_holdings.get(symbol, 0.0)
            current_notional = current_qty * last_price(symbol)
            need_notional = tgt_notional - current_notional
            if need_notional >= 1.0:  # Alpaca's $1 fractional minimum
                self.submit_and_wait(
                    symbol, need_notional, "buy",
                    price_hint=last_price(symbol),
                )

        # 7. Final state snapshot.
        log.info("-" * 60)
        post_holdings = self.get_current_holdings()
        post_dm = {s: q for s, q in post_holdings.items() if s in self.universe}
        log.info("Post-rebalance total equity: $%.2f", self.get_equity())
        log.info("Post-rebalance DM holdings:  %s",
                 post_dm if post_dm else "(none)")
        log.info("=" * 60)

    # ------------------------------------------------------------------ #
    # Entry point                                                        #
    # ------------------------------------------------------------------ #
    def run(self, force: bool = False) -> None:
        log.info("GEM live runner starting -- universe=%s safe=%s top_n=%d "
                 "lookback=%d threshold=%+.1f%%",
                 self.risky_universe, self.safe_asset, self.top_n,
                 self.lookback, self.threshold * 100)
        log.info("Endpoint: %s%s", config.BASE_URL,
                 " [DRY-RUN]" if self.dry_run else "")

        if force:
            log.warning("--force flag set; running rebalance regardless of date.")
            self.rebalance()
            return

        if not self.is_today_month_end():
            log.info("Today is not a month-end trading day; nothing to do.")
            return

        log.info("Today IS the last trading day of the month; rebalancing.")
        self.rebalance()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run GEM dual momentum on Alpaca paper.")
    parser.add_argument("--force", action="store_true",
                        help="Bypass the month-end check and rebalance now (testing).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Go through the full flow but do NOT submit any orders.")
    parser.add_argument("--preview", action="store_true",
                        help="Show what the strategy would do RIGHT NOW. Implies "
                             "--dry-run --force and skips the market-hours check.")
    args = parser.parse_args()

    # --preview implies --dry-run + --force, and skips clock check.
    dry_run = args.dry_run or args.preview
    force = args.force or args.preview

    try:
        GEMLiveRunner(dry_run=dry_run).run(force=force)
    except RuntimeError as exc:
        log.error(str(exc))
        return 1
    except Exception as exc:
        log.exception("Unexpected error: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
