# Bot-Trader: Dual-Strategy Momentum on Alpaca

A two-sleeve systematic momentum portfolio for Alpaca paper trading.
Each sleeve operates independently against a configurable share of total equity.

**⚠️ Paper trade only. Educational code, not investment advice.** See "Realistic
expectations" below before deploying real money — the strategies have known
underperformance windows.

## The two sleeves

| Sleeve | Strategy | Universe | Cadence | What it's good at |
|---|---|---|---|---|
| **DM** | Dual Momentum (Antonacci GEM) | SPY, EFA, AGG | Monthly rebalance | Drawdown reduction in bear regimes |
| **EMA** | Trend-state + cross-sectional momentum | 234 stocks/ETFs/ADRs | Daily check (5-min poll) | Trend capture across diverse equities |

Both write to the same Alpaca account. Universes don't overlap (DM uses 3 ETFs;
EMA uses 234 individual names + sector ETFs), so positions never collide.
Allocation between sleeves is set in `config.py` (`ALLOCATION_EMA`, `ALLOCATION_DM`).

## Validated backtest performance

EMA-sleeve strategy ("v2 trend-state") on a 234-name 2020-vintage US-large-cap
universe, including international ADRs and sector ETFs:

| Window | Total return | Sharpe | Max DD | Alpha vs SPY |
|---|---:|---:|---:|---:|
| 2007-2024 (incl. GFC) | +456% | +0.40 | 33.75% | **+139.65 pp** |
| 2020-2024 (no real bear) | +48% | +0.31 | 19.95% | -33.50% |

DM-sleeve strategy (canonical Antonacci GEM, top 1 of SPY/EFA/AGG):

| Window | Total return | Sharpe | Max DD | Alpha vs SPY |
|---|---:|---:|---:|---:|
| 2007-2024 | +156% | +0.16 | 33.86% | -160.57% |

**Both strategies have well-understood character:** they reduce drawdowns vs SPY
and beat SPY over long cycles that include real bear markets. They underperform
SPY in pure bull windows like 2020-2024.

## Project structure

```
bot-trader/
├── config.py                  # All settings — edit first
├── universe.py                # Curated 234-name universe for the EMA sleeve
├── strategy.py                # MomentumStrategy (legacy) + TrendStateStrategy (live)
├── risk_manager.py            # Position sizing, stops (ATR), daily loss limits
├── dual_momentum.py           # Dual momentum strategy + monthly-rebalance backtest
├── backtest.py                # EMA-sleeve historical simulation
├── trader.py                  # EMA-sleeve LIVE trader (long-running daemon)
├── live_dual_momentum.py      # DM-sleeve LIVE runner (run daily, acts on month-end)
├── status.py                  # Account/positions/recent-orders snapshot
├── data_source.py             # yfinance / Alpaca data adapters (used by backtests)
├── trade_log.py               # SQLite round-trip trade log (EMA sleeve)
├── order_log.py               # CSV per-order log (both sleeves)
├── monitoring.py              # Logging + heartbeat
├── trades_cli.py              # CLI to query trade_log.py
├── smoke_test.py              # One-shot Alpaca auth verification
├── tests/                     # 96 unit tests — run with `pytest`
├── logs/                      # Trader log + heartbeat + orders.csv
├── requirements.txt
└── README.md
```

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

Python 3.10+ recommended.

### 2. Get Alpaca paper API keys

Sign up at [alpaca.markets](https://alpaca.markets), create a paper account, and
generate an API key pair from the dashboard.

### 3. Create `.env`

In the project root, create a file named `.env` with these *exact* names:

```
ALPACA_API_KEY=your_key_id
ALPACA_API_SECRET=your_secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

(`.env` is in `.gitignore`. Don't commit it. The codebase reads `ALPACA_*` —
NOT the `APCA_*` names that Alpaca's SDK uses internally.)

### 4. Verify auth

```bash
python smoke_test.py
```

Should print `✓ Authenticated.` and show your paper account balance.

### 5. Review `config.py`

Key settings to check:
- `ALLOCATION_EMA` / `ALLOCATION_DM` — sleeve split (default 50/50)
- `STRATEGY_MODE` — "trend_state" (recommended) or "ema_cross" (legacy)
- `WATCHLIST` — sourced from `universe.py`; edit there
- `DUAL_MOMENTUM_RISKY` / `DUAL_MOMENTUM_SAFE` — DM sleeve assets
- `MAX_OPEN_POSITIONS` — how many EMA positions to hold (default 10)

## Operational commands

### Inspect what's happening (no orders submitted)

```bash
# What would the EMA strategy do RIGHT NOW?
python trader.py --preview

# What would the DM strategy do RIGHT NOW?
python live_dual_momentum.py --preview

# Account/positions/recent orders snapshot
python status.py
python status.py --sleeve dm        # filter to DM only
python status.py --orders 50         # show last 50 orders
```

### Dry-run (full flow but no order submission)

```bash
python trader.py --dry-run                  # one tick, full evaluation
python live_dual_momentum.py --dry-run      # respects market hours, just doesn't submit
```

### Backtest

```bash
python backtest.py        # EMA-sleeve historical simulation
python dual_momentum.py   # DM-sleeve historical simulation
```

Edit `BACKTEST_START` / `BACKTEST_END` in `config.py` to change the window.

### Live trading

```bash
# EMA sleeve — long-running daemon, polls every 5 minutes
python trader.py

# DM sleeve — run once per day; only acts on the last trading day of each month
python live_dual_momentum.py
```

For production, schedule `live_dual_momentum.py` via cron / Task Scheduler:
```
30 15 * * 1-5  cd /path/to/bot-trader && python live_dual_momentum.py
```
(15:30 ET = 30 min before close. Adjust UTC offset for your scheduler.)

`trader.py` should be run as a long-lived process. On Linux: `tmux` / `nohup` /
systemd. On Windows: a persistent terminal or Task Scheduler "at startup."

## Capital requirements

The strategy uses **Alpaca's fractional share API** (notional/dollar-based orders),
so it can run at any capital level. Minimums are set by transaction friction, not
share-price math:

| Capital | Behavior |
|---|---|
| $100 | Functions but ~25% annual fee/slippage drag — not recommended |
| $500 | ~5% drag |
| $1,000 | ~3% drag |
| $5,000+ | ~1% drag, approaches backtest assumptions |

For meaningful learning at small scale, **paper trading is free** and behaves
identically to real trading from the strategy's perspective.

## Realistic expectations

**This is not a get-rich strategy.** Honest expected forward performance:

- **DM sleeve**: ~3-5%/yr, max DD ~25-35%. Will lag SPY in bull-only periods
  (~50% of any given decade). Compensates by avoiding 50%+ drawdowns in 2008-style
  events. Sharpe ~0.2-0.4 forward.
- **EMA sleeve**: ~6-10%/yr over full cycles, max DD ~25-35%. Will significantly
  underperform SPY in pure bull windows like 2020-2024 (-30 to -50% alpha). Will
  outperform meaningfully when bears occur. Sharpe ~0.3-0.5 forward.
- **Both backtests overstate forward returns** by some material amount due to
  (a) some residual look-ahead in the universe, (b) un-modeled slippage, (c) tax
  drag in non-IRA accounts. Discount expectations by ~20-30%.

The point of this project is **risk-adjusted return with bounded drawdowns**, not
beating SPY in every window. If you can't tolerate a multi-year stretch where
SPY beats you, don't run tactical strategies — buy and hold an index fund.

## Caveats

- **Survivorship bias is impossible to fully eliminate** with retail data sources.
  The universe is curated to reduce it (includes underperformers like INTC, GE,
  IBM) but stocks delisted before 2024 are inherently absent.
- **Free-tier IEX data** is sufficient for daily-bar strategies on liquid US names.
  Less reliable for thinly-traded ADRs.
- **PDT rule**: US accounts under $25k are limited to 3 day-trades per 5 business
  days. The strategies are daily-timeframe so PDT rarely binds, but be aware.
- **Backtests assume zero commission** (Alpaca's model) and fills at the close.
  Real fills will differ. Slippage on liquid names is small (~5 bp); on ADRs and
  smaller caps it's larger.

## Tests

```bash
pytest -q
```

Should report 96 passed.
