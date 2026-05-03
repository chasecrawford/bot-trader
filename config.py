"""
config.py — Central configuration for the momentum trader.

All tunable settings live here so you don't have to hunt through the codebase
to change a stop-loss threshold or swap symbols. Edit this file, not the others.
"""

import os

# Load variables from a local .env file (if present) into os.environ before we
# read them below. Shell-exported env vars still take precedence. dotenv is
# optional — if it isn't installed, we silently fall back to the real shell env.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Alpaca API credentials
# ---------------------------------------------------------------------------
# Get these from https://alpaca.markets after creating a paper trading account.
# Prefer environment variables (or a .env file) so you don't accidentally
# commit secrets to git. .env is already in .gitignore.
# ---------------------------------------------------------------------------
API_KEY = os.getenv("ALPACA_API_KEY", "YOUR_API_KEY_HERE")
API_SECRET = os.getenv("ALPACA_API_SECRET", "YOUR_API_SECRET_HERE")

# Paper trading endpoint. Switch to "https://api.alpaca.markets" only after
# you've tested thoroughly with paper money.
BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# ---------------------------------------------------------------------------
# Multi-strategy allocation
# ---------------------------------------------------------------------------
# Both strategies share the same Alpaca account. Each operates on a fraction
# of total equity for sizing/risk decisions. Universes don't overlap (EMA-cross
# trades individual stocks; dual momentum trades 3 ETFs), so positions never
# collide. Must sum to <= 1.0; remainder sits in cash.
ALLOCATION_EMA = 1.00    # 100% to EMA sleeve (no DM split for this experiment)
ALLOCATION_DM  = 0.00    # DM sleeve disabled

# ---------------------------------------------------------------------------
# Watchlist — the eligible universe of symbols the strategy can trade.
# ---------------------------------------------------------------------------
# Sourced from universe.py — ~234 symbols spanning all 11 GICS sectors plus
# sector ETFs and international ADRs. Inclusion methodology documented there.
# MAX_OPEN_POSITIONS (below) caps how many of these the strategy can hold
# simultaneously; CROSS_SECTIONAL_TOP_PCT determines which fraction of the
# universe is eligible for entry on any given day.
# ---------------------------------------------------------------------------
from universe import get_full_universe
WATCHLIST = get_full_universe()

# ---------------------------------------------------------------------------
# Strategy parameters
# ---------------------------------------------------------------------------
TIMEFRAME = "1Day"          # Bar size: "1Min", "5Min", "15Min", "1Hour", "1Day"

# Strategy selection -- "trend_state" (recommended) uses TrendStateStrategy
# (price-vs-MAs state); "ema_cross" uses MomentumStrategy (EMA crossover event).
STRATEGY_MODE = "trend_state"

# TrendStateStrategy parameters
STATE_FAST_MA = 50          # Fast moving average (typically 50-day)
STATE_SLOW_MA = 200         # Slow moving average (typically 200-day)

# MomentumStrategy (EMA-cross) parameters -- only used when STRATEGY_MODE = "ema_cross"
FAST_EMA = 12               # Fast exponential moving average period
SLOW_EMA = 26               # Slow exponential moving average period
RSI_PERIOD = 14             # RSI lookback
RSI_OVERBOUGHT = 70         # Avoid entries above this level
RSI_OVERSOLD = 30           # Avoid exits below this (or use for mean-reversion)
MIN_VOLUME = 1_000_000      # Skip symbols with daily volume below this

# Exit on momentum drop -- when a held position falls out of the cross-sectional
# top-N, exit it on next rebalance (don't wait for stop or signal change).
EXIT_ON_MOMENTUM_DROP = True

# Lookback (in bars) when requesting history for indicator calculation.
# Must be comfortably larger than SLOW_EMA to let the EMA stabilize.
# Also must exceed CROSS_SECTIONAL_LOOKBACK_DAYS for the filter to engage.
HISTORY_BARS = 250

# Cross-sectional momentum filter (Jegadeesh-Titman style).
# Only allows entries on symbols ranked in the top CROSS_SECTIONAL_TOP_PCT
# of the universe by trailing CROSS_SECTIONAL_LOOKBACK_DAYS return. Combines
# with the per-symbol EMA signal — a stock must pass BOTH to be entered.
CROSS_SECTIONAL_ENABLED = True
CROSS_SECTIONAL_LOOKBACK_DAYS = 126   # ~6 months of trading days
CROSS_SECTIONAL_TOP_PCT = 0.10        # Top decile (was 0.20) — uses wider universe selectivity

# Market regime filter — only enter new longs when the broad market is bullish
# (benchmark close > its trailing SMA). Existing positions are NOT force-closed
# when regime turns bearish; per-position stops handle exits.
MARKET_REGIME_ENABLED = True
MARKET_REGIME_SYMBOL = "SPY"          # Benchmark to gauge market regime
MARKET_REGIME_MA_DAYS = 200           # Classic "price > 200-day MA" filter

# ---------------------------------------------------------------------------
# Dual Momentum (Antonacci) — separate strategy from EMA-cross above.
# Used by dual_momentum.py, not by trader.py / backtest.py.
# ---------------------------------------------------------------------------
# Risky asset universe spanning major asset classes — strategy picks top N
# by trailing 12-month return, conditional on absolute momentum being
# positive (return > threshold).
DUAL_MOMENTUM_RISKY = ["SPY", "EFA"]  # Canonical GEM: US stocks vs intl developed
DUAL_MOMENTUM_SAFE = "AGG"            # Where capital goes when no risky asset qualifies
DUAL_MOMENTUM_LOOKBACK_DAYS = 252     # 12 months of trading days
DUAL_MOMENTUM_TOP_N = 1               # GEM: concentrated bet — hold the single winner
DUAL_MOMENTUM_THRESHOLD = 0.0         # Absolute momentum hurdle (0.0 = "must be positive")
DUAL_MOMENTUM_REBALANCE = "month_end" # When to rebalance: only "month_end" supported for now

# ---------------------------------------------------------------------------
# Risk management
# ---------------------------------------------------------------------------
MAX_POSITION_PCT = 0.09       # Max % of equity in a single position. With MAX_OPEN_POSITIONS=10 = 90% max gross exposure, leaving 10% buffer to prevent accidental margin use from price drift / rounding.
MAX_PORTFOLIO_RISK = 0.02     # Risk per trade as % of equity (2%)

# ATR-based (volatility-adjusted) stops. Preferred when ATR is available from
# the strategy's indicator pass — each stock's stop distance then scales with
# its own typical daily range, rather than a one-size-fits-all percent.
ATR_PERIOD = 14               # Lookback for Wilder's ATR
ATR_STOP_MULT = 2.5           # Hard stop at entry - 2.5 * ATR(at entry)
ATR_TRAIL_MULT = 6.0          # Trailing stop at hwm - 6.0 * ATR(at entry); 0 disables

# Percent-based fallbacks — used when ATR isn't available (e.g. insufficient
# bar history on entry, or legacy positions adopted without an ATR value).
STOP_LOSS_PCT = 0.05          # Hard stop: exit if price drops 5% below entry
TRAILING_STOP_PCT = 1.0       # Percent trail disabled; ATR trail is primary
MAX_DAILY_LOSS_PCT = 0.03     # Halt trading for the day at 3% portfolio loss
MAX_OPEN_POSITIONS = 10       # Cap simultaneous positions; with MAX_POSITION_PCT=0.09 = 90% gross deployed when 10 signals fire (10% cash buffer prevents margin use)

# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
POLL_INTERVAL_SEC = 300       # 5-min poll: daily-bar signals don't need faster, lets us fit ~600 symbols within 200 req/min limit
ORDER_TYPE = "market"         # "market" or "limit"
TIME_IN_FORCE = "day"         # "day", "gtc", "ioc", "fok"

# ---------------------------------------------------------------------------
# Backtesting
# ---------------------------------------------------------------------------
BACKTEST_START = "2007-01-01"
BACKTEST_END = "2026-04-21"
BACKTEST_INITIAL_CAPITAL = 1_000.0
BACKTEST_COMMISSION = 0.0     # Alpaca is commission-free; raise to model slippage

# "yfinance" (free, default) or "alpaca" (requires API keys). yfinance lets
# you iterate on strategy parameters without hitting Alpaca's data API.
BACKTEST_DATA_SOURCE = "yfinance"

# Symbol to use as a passive buy-and-hold benchmark in the backtest report.
BENCHMARK_SYMBOL = "SPY"

# Risk-free rate (annualized decimal) used when computing Sharpe ratio.
# ~short-term T-bill yield is a reasonable default; adjust for your era.
RISK_FREE_RATE = 0.04

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
# Path to the SQLite database where both live and backtest trades are logged.
TRADE_LOG_DB = "trades.db"

# ---------------------------------------------------------------------------
# Operational / monitoring
# ---------------------------------------------------------------------------
# Directory for rotating log files. Created if it doesn't exist.
LOG_DIR = "logs"
LOG_FILE = "trader.log"

# Heartbeat JSON file — rewritten every tick. Point an external monitor
# (cron, uptime checker) at this file's mtime to detect a stuck bot.
HEARTBEAT_FILE = "heartbeat.json"

# Optional webhook for ERROR-level log alerts. Set the ALERT_WEBHOOK_URL
# environment variable (e.g. a Slack/Discord incoming webhook URL) to enable.
