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

19-year comparison (2007-04-21 to 2026-04-21) on $1,000 initial capital, all
returns include dividend reinvestment:

| Investment | Total return | Final value | Sharpe | Max DD |
|---|---:|---:|---:|---:|
| **EMA v2 strategy** | **+768%** | **$8,677** | **+0.48** | **34%** |
| SPY (total return, dividends reinvested) | +615% | $7,150 | ~0.55 | 56% |
| BIL (T-bills, "no-risk") | +29% | $1,286 | — | ~0% |

**EMA beats SPY total return by +153 percentage points over 19 years** with
nearly half SPY's max drawdown. Both beat T-bills by a huge margin (equity
risk premium). See `STRATEGY.md` for the full mechanism explanation.

**Important caveats:**
- These are gross backtest results; real-world friction reduces returns by
  0.5-1% per year at $5k+ capital, more at smaller scale.
- The strategy underperforms SPY in pure-bull sub-windows (e.g., 2020-2024).
  Win comes over full cycles that include bear markets.
- Short-term capital gains tax can add 2-5% annual drag in a taxable account.

## Paper-trading experience

The EMA sleeve has been running on Alpaca's paper trading endpoint
(`ALLOCATION_EMA = 1.0`, `ALLOCATION_DM = 0.0`). Two non-obvious bugs
surfaced in live execution that the backtest didn't catch — both are
fixed in tree, but they're documented here because they're exactly
the kind of issues anyone running this code is likely to think about:

- **Re-entry too soon after a stop-out.** When a position stopped out
  and a buy signal re-fired the next day, the strategy would buy back
  in immediately — converting the stop-loss into churn. Now blocked
  for at least 5 calendar days **and** until price recovers above the
  stop level (hard expiry at 30 days), tracked per-symbol in
  `stop_cooldown.py`. See commit `fix: buy cooldown after stop`.
- **Position cap selected alphabetically, not by conviction.** When
  more than `MAX_OPEN_POSITIONS` (10) signals fired the same day, the
  cap was filled in `bars_by_symbol` insertion order, which happened
  to be alphabetical — so AAPL would beat NVDA on a tied day even when
  NVDA had stronger momentum. Eligible buys are now sorted by
  trailing-return momentum score before the cap is applied
  (`trader.py:518`). See commit `fixes`.

These are the kind of bugs that **only show up under live daily
execution**: backtest never simulates the next-tick re-entry path
crisply enough to expose the first, and `bars_by_symbol` ordering
happened to align well enough with momentum strength on backtest dates
to mask the second. If you hardstop your trust at "the backtest looks
clean" you'll ship them.

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
├── equity_history.py          # Daily equity snapshots (idempotent per UTC date)
├── monitoring.py              # Logging + heartbeat
├── trades_cli.py              # CLI to query trade_log.py + equity snapshots
├── smoke_test.py              # One-shot Alpaca auth verification
├── tests/                     # 133 unit tests — run with `pytest`
├── logs/                      # Trader log + heartbeat + orders.csv
├── requirements.txt
├── README.md
├── STRATEGY.md                # EMA v2 strategy deep-dive
└── LICENSE
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

Copy the template and fill in your keys:

```bash
cp .env.example .env
# then edit .env with your real paper-trading keys
```

The file needs these *exact* variable names (not the `APCA_*` names used by
Alpaca's SDK internally):

```
ALPACA_API_KEY=your_key_id
ALPACA_API_SECRET=your_secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

`.env` is in `.gitignore`; never commit real keys.

### 4. Verify auth

```bash
python smoke_test.py
```

Should print `✓ Authenticated.` and show your paper account balance.

### 5. Review `config.py`

Key settings to check:
- `ALLOCATION_EMA` / `ALLOCATION_DM` — sleeve split. Ships 1.0/0.0 (EMA-only,
  the validated configuration); set to 0.5/0.5 to run both sleeves or 0.0/1.0
  for DM-only
- `STRATEGY_MODE` — "trend_state" (recommended) or "ema_cross" (legacy)
- `WATCHLIST` — sourced from `universe.py`; edit there
- `DUAL_MOMENTUM_RISKY` / `DUAL_MOMENTUM_SAFE` — DM sleeve assets
- `MAX_OPEN_POSITIONS` — how many EMA positions to hold (default 10)
- `MAX_POSITION_PCT` — max % of sleeve per position (default 0.09).
  With 10 positions, this gives 90% max gross exposure — a 10% safety buffer
  against accidentally using margin.

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

### Equity tracking

`trader.py` records one row per UTC day into the `equity_snapshots`
table in `trades.db` on each heartbeat. The `UNIQUE (date_utc, sleeve)`
constraint makes this idempotent — first tick of the day inserts,
later same-day ticks are no-ops, and a restart in the middle of the
day doesn't corrupt the series.

Dump the recorded series as JSON for downstream consumers (e.g. a
website chart):

```bash
# 7 most recent calendar days, total equity, JSON to stdout
python trades_cli.py equity-history

# 5 most recent trading days (Sat/Sun excluded — better for charts since
# weekends are no-op equity holds). Holidays aren't excluded; the count
# is approximate at long horizons.
python trades_cli.py equity-history --trading-days 5

# Custom window + write to a file (good for scheduled refreshes)
python trades_cli.py equity-history --trading-days 5 --output paper-equity.json

# Dump the EMA sleeve's series instead of total
python trades_cli.py equity-history --sleeve ema
```

`--days` and `--trading-days` are mutually exclusive — pick one.

Output shape:

```json
{
  "start_date": "2026-04-22",
  "as_of": "2026-05-03T12:34:56+00:00",
  "snapshots": [
    {"date": "2026-04-27", "equity": 5012.34},
    {"date": "2026-04-28", "equity": 5018.91}
  ],
  "positions": ["AAPL", "MSFT", "NVDA"]
}
```

- `start_date` — earliest snapshot recorded for the requested sleeve;
  useful for labeling charts as "since YYYY-MM-DD" without hardcoding.
- `positions` — currently-held symbols. Sourced from Alpaca's
  `list_positions()` API call (live broker state, source of truth).
  Falls back to `heartbeat.json`'s `open_positions` map if the API call
  fails (no creds, network down, etc.) — override the heartbeat path
  with `--heartbeat PATH`. Empty list if both sources fail.

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

#### Graceful shutdown (cross-platform)

`trader.py` polls for a sentinel file `logs/STOP` on every tick. Creating that
file makes the trader finish its current iteration and exit cleanly — no
SIGKILL, no half-submitted orders. The mechanism is pure Python and works on
any OS:

```bash
# Linux/macOS — graceful stop:
touch logs/STOP

# Windows — graceful stop:
New-Item -Path "logs\STOP" -ItemType File -Force
```

Pair this with your scheduler of choice. The `scripts/` directory ships
ready-made **Windows Task Scheduler** wrappers (`start_trader.ps1`,
`stop_trader.ps1`, `register_tasks.ps1`); see `scripts/README.md`. On Linux,
build the equivalent with a systemd service + timer or a cron pair; on macOS,
launchd or cron. The portable primitive is the `logs/STOP` sentinel —
everything else is just a scheduler-specific wrapper around it.

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

## Margin safety

Alpaca paper and real accounts default to **margin accounts** with 2× buying power.
This project has **three layers of protection** against accidentally using margin:

1. `MAX_POSITION_PCT = 0.09` with `MAX_OPEN_POSITIONS = 10` → 90% max gross
   exposure, leaving 10% cash buffer against price drift.
2. **Hard pre-submission check in `trader.py`**: refuses any order that would
   exceed available cash (you'll see a log line: `Skip XYZ: would use margin`).
3. Position sizing uses `equity`, not `buying_power`, throughout.

The buying-power number Alpaca displays will be 2× your cash, but **the strategy
will never use the margin half** — guaranteed by code + config.

## Caveats

- **Survivorship bias is impossible to fully eliminate** with retail data sources.
  The universe is curated to reduce it (includes underperformers like INTC, GE,
  IBM) but stocks delisted before 2024 are inherently absent.
- **Free-tier IEX data** is sufficient for daily-bar strategies on liquid US names.
  Less reliable for thinly-traded ADRs.
- **PDT rule**: US accounts under $25k are limited to 3 day-trades per 5 business
  days. The strategies are daily-timeframe so PDT rarely binds, but be aware.
- **Backtests use yfinance dividend-adjusted prices** (since a recent fix) so both
  strategy returns and the SPY benchmark reflect total return. Commissions are $0
  on Alpaca; slippage and regulatory fees are NOT modeled and add ~0.5-1%/yr of
  friction in real deployment.

## Further reading

- `STRATEGY.md` — deep-dive on the EMA v2 strategy mechanics, why it works (and
  doesn't), and where each piece lives in code.

## Tests

```bash
pytest -q
```

Should report 97 passed.
