"""
opened_today.py — Persistent registry of symbols the bot bought today.

Drives the PDT guardrail in trader.py: a sell of a symbol the bot bought
on the current UTC date would create a day-trade. Persisting across
restarts lets a mid-session restart still recognize same-day round-trips
and avoid accidentally tripping the FINRA pattern-day-trader flag.
"""

import json
import logging
from datetime import date

log = logging.getLogger("trader")


class OpenedTodayRegistry:
    """Tracks symbols opened on a given UTC date. Persists to JSON."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._data: dict[str, str] = self._load()

    # ------------------------------------------------------------------ #
    # Persistence                                                         #
    # ------------------------------------------------------------------ #
    def _load(self) -> dict:
        try:
            with open(self._path) as f:
                data = json.load(f)
            # Defensive: only accept str->str mappings; ignore anything else.
            return {
                str(k): str(v)
                for k, v in data.items()
                if isinstance(k, str) and isinstance(v, str)
            }
        except FileNotFoundError:
            return {}
        except Exception as exc:
            log.warning(
                "Could not load opened-today file (%s): %s — starting fresh.",
                self._path, exc,
            )
            return {}

    def _save(self) -> None:
        try:
            with open(self._path, "w") as f:
                json.dump(self._data, f, indent=2)
        except Exception as exc:
            log.warning(
                "Could not save opened-today file (%s): %s", self._path, exc,
            )

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #
    def prune(self, today: date) -> None:
        """Drop any entry whose stored date isn't `today`. Saves if changed."""
        today_iso = today.isoformat()
        stale = [s for s, d in self._data.items() if d != today_iso]
        if stale:
            for s in stale:
                del self._data[s]
            self._save()

    def record_open(self, symbol: str, today: date) -> None:
        self._data[symbol] = today.isoformat()
        self._save()

    def record_close(self, symbol: str) -> None:
        if self._data.pop(symbol, None) is not None:
            self._save()

    def was_opened_on(self, symbol: str, today: date) -> bool:
        return self._data.get(symbol) == today.isoformat()
