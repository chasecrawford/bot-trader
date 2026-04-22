# EMA v2: Trend-State Cross-Sectional Momentum

The long-only US equity momentum strategy that runs in the EMA sleeve.
This document is the strategy's reference; for setup and operations see `README.md`.

## What it is

A systematic strategy that holds up to 10 stocks selected from a 234-name universe
of US large/mid-caps, sector ETFs, and international ADRs. Entry requires a stock
to be (1) in a confirmed uptrend by its own price action, AND (2) ranked in the
top 10% of the universe by trailing 6-month return, AND (3) trading in a broadly
bullish market regime. Positions are sized by volatility, exit on multiple
triggers, and typically held 30-90 days.

The "EMA" in the name is a historical misnomer. The live signal is a moving-average
state check (price > 50DMA AND price > 200DMA), not an EMA crossover event. The
old EMA-cross strategy still exists in the code (toggleable via `STRATEGY_MODE`)
but is retained only for reference — it was validated as not having edge.

## How it decides what to buy

Three filters in series — a stock must pass all three:

**1. Per-stock signal: in a confirmed uptrend.**
The current price must be above both the 50-day and 200-day simple moving averages.
This is a *state* (true for weeks at a time), not an *event* (a momentary crossover).
A pure event-trigger like an EMA cross gets knocked out by noise; a state filter
stays in trends as long as they're intact. Implemented in
`strategy.py:TrendStateStrategy.generate_signal`.

**2. Cross-sectional momentum: in top 10% of universe.**
The stock's trailing 126-day (6-month) total return must rank in the top 10% of
the 234-name universe — about 23 candidates at any moment. This implements the
Jegadeesh-Titman effect (1993): stocks that have outperformed over 6-12 months
tend to keep outperforming for the next 1-3 months. Implemented in
`strategy.py:cross_sectional_momentum_filter`.

**3. Market regime: SPY in confirmed uptrend.**
No new entries when SPY is below its 200-day moving average. This skips trading
entirely during broad bear markets like 2008 or 2022. Existing positions are NOT
force-closed when regime turns bearish — they manage out via per-position stops.
Implemented in `strategy.py:market_regime_bullish`.

A stock passing all three is bought at market on the next tick.

## How it decides what to sell

Four exit triggers, whichever fires first:

- **ATR trailing stop**: position closes if price falls more than 6 × ATR(14) below
  its highest mark since entry. The ATR is computed at entry time and *frozen*, so
  the stop distance reflects the stock's volatility *as it was when you bought it*.
  This avoids the failure mode where a sudden volatility spike tightens your stop
  against you mid-trade.
- **ATR hard stop**: position closes if price falls more than 2.5 × ATR below entry
  price. Caps maximum loss per trade at roughly the same dollar amount across
  positions of varying volatility.
- **Trend break**: position closes if price drops below the 200-day MA. The
  long-term uptrend that justified entry is broken.
- **Momentum drop-out**: position closes if it falls out of the cross-sectional
  top 10% by trailing return. This is the symmetric partner to the entry filter
  and what professional momentum funds call "rebalancing into current leaders."

A separate **daily loss limit** at 3% of sleeve equity halts *new entries* (but
doesn't force exits) for the rest of the day — a circuit breaker against
pathological loss days.

## Position sizing

For each new entry, take the *smaller* of:
- 10% of sleeve equity (`MAX_POSITION_PCT`), and
- The size where a stop-out would cost 2% of sleeve equity (`MAX_PORTFOLIO_RISK`)

This keeps any single trade from being either too large in dollar terms or too
risky relative to its stop placement. Volatile stocks (high ATR → wider stop)
end up smaller; stable stocks (low ATR → tighter stop) end up at the 10% cap.

The portfolio is capped at 10 simultaneous positions (`MAX_OPEN_POSITIONS`). At
max 10% per position, the strategy is fully invested only when 10+ signals fire
concurrently. When fewer signals exist, the rest sits in cash by design.

## The universe

234 names curated as US large-and-mid-caps that were liquid as of January 2020.
Deliberately includes names that *subsequently underperformed* (Intel, GE, IBM,
AT&T, Citi, energy mid-caps) so the universe doesn't have hindsight bias toward
2024-known winners. Spans all 11 GICS sectors with deliberate over-weight on
cyclicals, plus 11 sector SPDR ETFs (XLK, XLF, etc.) and ~15 international ADRs
(TSM, ASML, NVO, BABA, SAP, etc.).

Defined in `universe.py` with full methodology comments. To add or remove names,
edit there and re-run any backtest.

## Performance character

Validated on 18 years (2007-01-01 to 2024-12-31) of yfinance daily bars on the
234-name universe:

| Metric | Strategy | SPY |
|---|---:|---:|
| Total return | **+456%** | +316% |
| Annualized return | ~10% | ~8% |
| Sharpe ratio | **+0.40** | +0.30 |
| Max drawdown | **34%** | 56% |
| Alpha vs SPY | **+140 pp** | — |
| Trades over 18 yr | 1,940 | — |
| Win rate | 41.6% | — |
| Avg win / avg loss | $200 / $102 | — |

**The 18-year picture is unambiguously good**: beats SPY on absolute return, Sharpe,
and drawdown simultaneously. Most of the alpha was earned during 2008-2009 (when
regime + drop-out exits avoided most of the GFC) and 2022 (when same mechanisms
avoided most of the bear). Over the long run the strategy compounds more capital
because it loses less in bear regimes.

**But on shorter sub-windows the picture is mixed.** On 2020-2024 specifically:

| Metric | Strategy | SPY |
|---|---:|---:|
| Total return | +48% | +81% |
| Sharpe | 0.31 | 0.48 |
| Max drawdown | 20% | 34% |
| Alpha vs SPY | -33 pp | — |

This window is essentially uninterrupted bull market (one shallow 2022 bear).
**Any tactical strategy with cash drag loses to buy-and-hold in a regime like
this.** The strategy still has positive Sharpe and meaningfully lower max DD,
but it doesn't beat SPY on return.

This is the honest character of momentum strategies: **outperform over full
cycles, underperform in bull-only sub-windows.** Whether you experience the
former or the latter depends on what part of the cycle you're in when you start.

## What this strategy is NOT

- **Not a market-timer.** It doesn't predict tops or bottoms; it reacts to what's
  already happening. It will be late getting in (signals fire after a trend is
  established) and late getting out (stops trigger after a trend reverses).
- **Not a stock-picker.** No fundamental analysis, no earnings reads, no thesis
  on whether a company is "undervalued." Just price action and rank.
- **Not a leverage strategy.** Maximum gross exposure is 100% of sleeve equity
  (10 positions × 10% each). No margin used.
- **Not high-frequency.** Typical hold is 30-90 days. Most live ticks generate
  zero new orders. The cron-friendly cadence is daily-ish, not minute-ly.
- **Not guaranteed to beat SPY.** Multi-year underperformance windows are
  inherent to the design.

## Why it works (when it works)

A few mechanisms compound:

- **Cross-sectional momentum is a real, persistent factor.** Documented across
  decades (Jegadeesh-Titman 1993, Asness 1997, AQR papers since). Stocks in the
  top decile of trailing 6-12 month returns outperform the bottom decile by
  roughly 1% per month gross.
- **State-based entry stays in trends longer than event-based.** The naive EMA
  crossover signal flips on every minor noise event; price > 50DMA AND > 200DMA
  is true for the entirety of a real uptrend.
- **ATR-scaled stops respect each stock's character.** A 5% stop on volatile
  semis (NVDA, AMD) is normal-day noise; a 5% stop on staid utilities (DUK, ED)
  is a genuine trend break. ATR adapts.
- **Drop-out exits prevent slow bleeds.** Without them, a position that has
  topped out keeps being held until a hard stop fires — by which point you've
  given back much of the run-up.
- **Regime filter avoids trading bear markets.** When everything is going down,
  cross-sectional momentum doesn't help (the "best" of a bad bunch is still
  losing money). Sitting it out is the right call.

## Why it doesn't always beat SPY

Equally important to understand:

- **Cash drag in pure bull regimes.** Any time the strategy is less than 100%
  invested in stocks (which is most of the time), it underperforms a strategy
  that *is* 100% invested whenever stocks are rising.
- **Stops fire on legitimate trends.** ATR stops, even at 2.5×/6× multipliers,
  sometimes exit positions that subsequently resume their trend. There's no
  perfect stop level — every choice is a tradeoff.
- **Top-10% selection is past performance, not perfect prediction.** Some 6-month
  winners go on to keep winning; others mean-revert. The strategy can only ride
  the persistence.
- **The 200-day MA regime filter is slow.** It exits the bear too late and
  re-enters too late, so it gives back some of the avoidance benefit during
  the recovery.
- **Bull markets are SPY's home turf.** When the market goes up uninterrupted,
  there's nothing for tactical strategies to differentiate on. SPY just wins.

## Where the code lives

| File | What's in it |
|---|---|
| `strategy.py` `TrendStateStrategy` | Entry/exit signal (state-based) |
| `strategy.py` `cross_sectional_momentum_filter` | Top-N ranking by trailing return |
| `strategy.py` `market_regime_bullish` | SPY > 200DMA check |
| `strategy.py` `atr` | Wilder's ATR (used by stops) |
| `risk_manager.py` `RiskManager` | Position sizing, ATR stops, daily loss limit |
| `universe.py` `get_full_universe` | 234-name list with sector taxonomy |
| `backtest.py` `BacktestEngine` | Historical simulation with the same code paths as live |
| `trader.py` `Trader` | Live daemon — run during market hours |
| `config.py` | All tunables (no code changes needed for parameter tuning) |

Tunable parameters in `config.py` (all respond without code changes):
- Signal: `STATE_FAST_MA`, `STATE_SLOW_MA`
- Cross-sectional: `CROSS_SECTIONAL_TOP_PCT`, `CROSS_SECTIONAL_LOOKBACK_DAYS`
- Regime: `MARKET_REGIME_ENABLED`, `MARKET_REGIME_MA_DAYS`, `MARKET_REGIME_SYMBOL`
- Stops: `ATR_STOP_MULT`, `ATR_TRAIL_MULT`, `ATR_PERIOD`
- Sizing: `MAX_POSITION_PCT`, `MAX_PORTFOLIO_RISK`, `MAX_OPEN_POSITIONS`
- Exits: `EXIT_ON_MOMENTUM_DROP`
- Allocation: `ALLOCATION_EMA` (fraction of total account)

## Realistic forward expectations

Three honest numbers to internalize before deploying:

1. **~6-10% annualized return** over a multi-year horizon. Lower than SPY's
   long-run ~10% but with materially better drawdowns.
2. **~25-35% max drawdown** in real bear markets. Better than SPY's ~50% in
   2008-style events but still painful.
3. **Underperformance vs SPY in any specific bull-only window.** Plan for
   12-36 months of trailing SPY at some point. If you can't tolerate that
   without abandoning the strategy at the worst possible time, run a passive
   index instead.

The point of this strategy is **risk-adjusted return with bounded drawdowns**,
not beating SPY in every window. The 50/50 split with the dual momentum sleeve
provides additional diversification — when one strategy is weak, the other often
isn't.
