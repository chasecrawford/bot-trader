"""
data_source.py — Pluggable historical bar data.

The backtest only needs daily closes. We default to yfinance because it's
free and doesn't require credentials, which means you can iterate on the
strategy before you've even set up an Alpaca account. Alpaca remains
available for cases where you want the same data pipeline as live trading.

Both implementations return a dict keyed by symbol, with each value a
DataFrame indexed by tz-naive daily timestamps and containing (at least)
a 'close' column.

Usage:

    source = get_data_source("yfinance")
    data = source.load(symbols=["AAPL", "SPY"], start="2022-01-01",
                       end="2024-12-31")
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Sequence

import pandas as pd


class DataSource(ABC):
    """Interface for historical bar sources."""

    @abstractmethod
    def load(self, symbols: Sequence[str], start: str, end: str
             ) -> Dict[str, pd.DataFrame]:
        """Return daily bars for each symbol over [start, end]."""


# --------------------------------------------------------------------------- #
# yfinance implementation                                                     #
# --------------------------------------------------------------------------- #
class YFinanceSource(DataSource):
    """Free, credential-less daily bars from Yahoo Finance."""

    def load(self, symbols: Sequence[str], start: str, end: str
             ) -> Dict[str, pd.DataFrame]:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise RuntimeError(
                "yfinance is not installed. Run `pip install yfinance` "
                "or switch to the Alpaca data source."
            ) from exc

        symbols = list(symbols)
        if not symbols:
            return {}

        # Batch download — one HTTP round-trip per group instead of one per
        # symbol. ~10x faster for 50+ symbols. Returns a single DataFrame with
        # MultiIndex columns: (field, ticker) e.g. ('Close', 'AAPL').
        # auto_adjust=True returns dividend-and-split-adjusted prices, so the
        # backtest reflects total return (an investor receiving and reinvesting
        # dividends). Without this, both held positions AND the SPY benchmark
        # would understate true performance.
        print(f"Fetching {len(symbols)} symbols from yfinance (batch)...")
        df = yf.download(
            tickers=symbols,
            start=start,
            end=end,
            progress=False,
            auto_adjust=True,
            group_by="ticker",
            threads=True,
        )
        if df is None or df.empty:
            return {}

        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

        result: Dict[str, pd.DataFrame] = {}
        if isinstance(df.columns, pd.MultiIndex):
            # group_by="ticker" gives outer level = ticker, inner = field.
            for symbol in symbols:
                if symbol not in df.columns.get_level_values(0):
                    continue
                sub = df[symbol].dropna(how="all").copy()
                if sub.empty:
                    continue
                sub.columns = [str(c).lower() for c in sub.columns]
                result[symbol] = sub.sort_index()
        else:
            # Single-symbol response — yfinance flattens the columns.
            sub = df.copy()
            sub.columns = [str(c).lower() for c in sub.columns]
            result[symbols[0]] = sub.sort_index()

        return result


# --------------------------------------------------------------------------- #
# Alpaca implementation                                                       #
# --------------------------------------------------------------------------- #
class AlpacaSource(DataSource):
    """Historical daily bars from Alpaca — requires API credentials."""

    def __init__(self, api_key: str, api_secret: str, base_url: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url

    def load(self, symbols: Sequence[str], start: str, end: str
             ) -> Dict[str, pd.DataFrame]:
        from alpaca_trade_api.rest import REST, TimeFrame

        api = REST(key_id=self.api_key, secret_key=self.api_secret,
                   base_url=self.base_url)
        result: Dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            print(f"Fetching {symbol} from Alpaca...")
            bars = api.get_bars(symbol, TimeFrame.Day,
                                start=start, end=end).df
            if "symbol" in bars.columns:
                bars = bars[bars["symbol"] == symbol]
            if bars.empty:
                continue
            if bars.index.tz is not None:
                bars.index = bars.index.tz_localize(None)
            result[symbol] = bars.sort_index()
        return result


# --------------------------------------------------------------------------- #
# Factory                                                                     #
# --------------------------------------------------------------------------- #
def get_data_source(name: str = "yfinance", **kwargs) -> DataSource:
    """Construct a data source by name. Defaults to yfinance."""
    name = name.lower()
    if name == "yfinance":
        return YFinanceSource()
    if name == "alpaca":
        import config
        return AlpacaSource(
            api_key=kwargs.get("api_key") or config.API_KEY,
            api_secret=kwargs.get("api_secret") or config.API_SECRET,
            base_url=kwargs.get("base_url") or config.BASE_URL,
        )
    raise ValueError(f"Unknown data source: {name!r} (expected 'yfinance' or 'alpaca')")
