"""
stop_cooldown.py — Persistent cooldown registry for stopped-out positions.

After a hard stop fires, the same symbol is blocked from re-entry until ALL
of the following are true:
  1. At least MIN_COOLDOWN_DAYS calendar days have elapsed since the stop.
  2. The symbol's price has recovered above the stop level at exit.
  3. Fewer than MAX_COOLDOWN_DAYS have elapsed (hard expiry / safety valve).

The momentum signal is computed over months, so it's unlikely to change
within a day or two of a stop firing — without this gate the bot would
immediately re-buy the stopped symbol at a worse price (yo-yo trading).

State is persisted to a JSON file so cooldowns survive the daily restart.
"""

import json
import logging
from datetime import date

log = logging.getLogger("trader")

MIN_COOLDOWN_DAYS = 5   # Must wait at least this many calendar days.
MAX_COOLDOWN_DAYS = 30  # Always unblock after this many days regardless of price.


class StopCooldownRegistry:
    """Tracks symbols that were stopped out and gates their re-entry."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._data: dict = self._load()

    # ------------------------------------------------------------------ #
    # Persistence                                                         #
    # ------------------------------------------------------------------ #
    def _load(self) -> dict:
        try:
            with open(self._path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as exc:
            log.warning(
                "Could not load stop cooldown file (%s): %s — starting fresh.",
                self._path, exc,
            )
            return {}

    def _save(self) -> None:
        try:
            with open(self._path, "w") as f:
                json.dump(self._data, f, indent=2)
        except Exception as exc:
            log.warning(
                "Could not save stop cooldown file (%s): %s", self._path, exc,
            )

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #
    def record_stop(self, symbol: str, stop_price: float, today: date) -> None:
        """Record that symbol was stopped out today at the given stop level."""
        self._data[symbol] = {
            "stop_date": today.isoformat(),
            "stop_price": stop_price,
        }
        self._save()
        log.info(
            "Stop cooldown: %s blocked for >=%d days and until price recovers "
            "above %.2f (hard expiry after %d days).",
            symbol, MIN_COOLDOWN_DAYS, stop_price, MAX_COOLDOWN_DAYS,
        )

    def is_blocked(self, symbol: str, current_price: float, today: date) -> bool:
        """
        Return True if the symbol should be blocked from re-entry.

        Cleans up and saves when a cooldown expires or lifts naturally.
        """
        if symbol not in self._data:
            return False

        entry = self._data[symbol]
        stop_date = date.fromisoformat(entry["stop_date"])
        stop_price = entry["stop_price"]
        days_elapsed = (today - stop_date).days

        # Hard expiry — unblock unconditionally after MAX_COOLDOWN_DAYS.
        if days_elapsed >= MAX_COOLDOWN_DAYS:
            log.info(
                "Stop cooldown expired for %s (%d days elapsed — hard expiry).",
                symbol, days_elapsed,
            )
            del self._data[symbol]
            self._save()
            return False

        # Minimum cooldown period not yet elapsed.
        if days_elapsed < MIN_COOLDOWN_DAYS:
            return True

        # Price recovery gate — must close above the stop level at exit.
        if current_price <= stop_price:
            return True

        # Both conditions met: minimum time elapsed and price has recovered.
        log.info(
            "Stop cooldown lifted for %s: price %.2f recovered above "
            "stop %.2f after %d days.",
            symbol, current_price, stop_price, days_elapsed,
        )
        del self._data[symbol]
        self._save()
        return False

    def active_cooldowns(self) -> dict:
        """Return a snapshot of all active cooldown entries (for heartbeat/logging)."""
        return dict(self._data)
