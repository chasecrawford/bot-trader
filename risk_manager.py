"""
risk_manager.py — Position sizing and risk guardrails.

Trading strategies make money; risk management keeps it. This module decides
how much to buy, when to cut losers, and when to stop trading for the day.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Optional

import config


@dataclass
class PositionState:
    """Track per-position info we need for trailing stops and exits."""
    symbol: str
    entry_price: float
    qty: float
    high_water_mark: float  # Highest price seen since entry — drives trailing stop
    # Frozen at entry. When None, the RiskManager falls back to percent-based
    # stops. Freezing (vs. recomputing each bar) keeps the stop level stable
    # across mid-trade volatility regime changes.
    atr_at_entry: Optional[float] = None

    def update_high_water(self, price: float) -> None:
        if price > self.high_water_mark:
            self.high_water_mark = price


@dataclass
class RiskManager:
    """Enforces position sizing, stop-losses, and daily loss limits."""

    max_position_pct: float = config.MAX_POSITION_PCT
    max_portfolio_risk: float = config.MAX_PORTFOLIO_RISK
    stop_loss_pct: float = config.STOP_LOSS_PCT
    trailing_stop_pct: float = config.TRAILING_STOP_PCT
    atr_stop_mult: float = getattr(config, "ATR_STOP_MULT", 2.5)
    atr_trail_mult: float = getattr(config, "ATR_TRAIL_MULT", 4.0)
    max_daily_loss_pct: float = config.MAX_DAILY_LOSS_PCT
    max_open_positions: int = config.MAX_OPEN_POSITIONS

    # Runtime state
    positions: Dict[str, PositionState] = field(default_factory=dict)
    day_start_equity: Optional[float] = None
    current_day: Optional[date] = None
    trading_halted: bool = False

    # ------------------------------------------------------------------ #
    # Daily accounting                                                   #
    # ------------------------------------------------------------------ #
    def start_day(self, equity: float, today: date) -> None:
        """Call once per trading day before any signals are evaluated."""
        if self.current_day != today:
            self.current_day = today
            self.day_start_equity = equity
            self.trading_halted = False

    def check_daily_loss(self, current_equity: float) -> bool:
        """Return True if the daily loss limit has been breached."""
        if self.day_start_equity is None:
            return False
        drawdown = (self.day_start_equity - current_equity) / self.day_start_equity
        if drawdown >= self.max_daily_loss_pct:
            self.trading_halted = True
            return True
        return False

    # ------------------------------------------------------------------ #
    # Position sizing                                                    #
    # ------------------------------------------------------------------ #
    def calculate_position_size(
        self, equity: float, price: float, stop_price: Optional[float] = None,
    ) -> float:
        """
        Return notional dollar amount to allocate to this position.

        Uses whichever is SMALLER:
          - Max position notional (% of equity allowed in one name)
          - Risk-based notional (% of equity willing to lose if stop is hit)

        Caller can convert to shares via `notional / price` (fractional) or
        pass directly to Alpaca's `notional=` parameter for fractional orders.
        """
        if price <= 0 or equity <= 0:
            return 0.0

        # Cap by absolute position size (% of equity)
        max_position_notional = equity * self.max_position_pct

        # Cap by risk if a stop price is provided. Risk per share × shares =
        # risk budget; convert risk-shares to notional via × price.
        if stop_price is not None and stop_price < price:
            risk_per_share = price - stop_price
            risk_budget = equity * self.max_portfolio_risk
            risk_shares = risk_budget / risk_per_share
            risk_notional = risk_shares * price
            return max(0.0, min(max_position_notional, risk_notional))

        return max(0.0, max_position_notional)

    # ------------------------------------------------------------------ #
    # Entry / exit gating                                                #
    # ------------------------------------------------------------------ #
    def can_open_position(self, symbol: str) -> bool:
        if self.trading_halted:
            return False
        if symbol in self.positions:
            return False  # Already holding
        if len(self.positions) >= self.max_open_positions:
            return False
        return True

    def register_entry(
        self, symbol: str, entry_price: float, qty: float,
        atr_at_entry: Optional[float] = None,
    ) -> None:
        self.positions[symbol] = PositionState(
            symbol=symbol,
            entry_price=entry_price,
            qty=qty,
            high_water_mark=entry_price,
            atr_at_entry=atr_at_entry,
        )

    def register_exit(self, symbol: str) -> None:
        self.positions.pop(symbol, None)

    # ------------------------------------------------------------------ #
    # Stop-loss checks                                                   #
    # ------------------------------------------------------------------ #
    def should_stop_out(self, symbol: str, current_price: float) -> Optional[str]:
        """
        Check both hard stop and trailing stop. Returns a reason string if
        the position should be closed, else None.
        """
        pos = self.positions.get(symbol)
        if pos is None:
            return None

        pos.update_high_water(current_price)

        hard_stop = self._hard_stop_price(pos)
        if current_price <= hard_stop:
            kind = "ATR" if self._has_atr(pos) else "pct"
            return (
                f"Hard stop hit [{kind}] (entry={pos.entry_price:.2f}, "
                f"stop={hard_stop:.2f}, price={current_price:.2f})"
            )

        trail_stop = self._trail_stop_price(pos)
        # Trailing stop only engages once it's above entry (i.e. we're in profit
        # far enough that trailing would lock in a gain, not compound a loss).
        if (
            trail_stop is not None
            and trail_stop > pos.entry_price
            and current_price <= trail_stop
        ):
            kind = "ATR" if self._has_atr(pos) else "pct"
            return (
                f"Trailing stop hit [{kind}] (hwm={pos.high_water_mark:.2f}, "
                f"stop={trail_stop:.2f}, price={current_price:.2f})"
            )

        return None

    def stop_price_for(
        self, entry_price: float, atr_at_entry: Optional[float] = None,
    ) -> float:
        """Initial hard-stop level. Used by position sizing before entry."""
        if atr_at_entry and atr_at_entry > 0:
            return entry_price - self.atr_stop_mult * atr_at_entry
        return entry_price * (1 - self.stop_loss_pct)

    # ------------------------------------------------------------------ #
    # Internal stop-price helpers                                        #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _has_atr(pos: PositionState) -> bool:
        return bool(pos.atr_at_entry and pos.atr_at_entry > 0)

    def _hard_stop_price(self, pos: PositionState) -> float:
        if self._has_atr(pos):
            return pos.entry_price - self.atr_stop_mult * pos.atr_at_entry  # type: ignore[operator]
        return pos.entry_price * (1 - self.stop_loss_pct)

    def _trail_stop_price(self, pos: PositionState) -> Optional[float]:
        # ATR trail — atr_trail_mult <= 0 disables trailing entirely.
        if self._has_atr(pos):
            if self.atr_trail_mult <= 0:
                return None
            return pos.high_water_mark - self.atr_trail_mult * pos.atr_at_entry  # type: ignore[operator]
        # Percent fallback; TRAILING_STOP_PCT >= 1.0 disables by definition
        # (can't drop 100% from the high-water mark without hitting zero).
        return pos.high_water_mark * (1 - self.trailing_stop_pct)
