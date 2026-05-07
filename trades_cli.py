"""
trades_cli.py — Inspect the SQLite trade log without writing SQL.

Examples:

    python trades_cli.py summary
    python trades_cli.py summary --source backtest
    python trades_cli.py list --symbol AAPL --limit 20
    python trades_cli.py list --source live --since 2024-01-01
    python trades_cli.py export trades.csv --run-id bt-20250419-120000

Reads the DB path from config.TRADE_LOG_DB by default, overridable with --db.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from typing import List, Optional

# REST is imported at module level (rather than lazily inside the position
# helper) so tests can monkey-patch `trades_cli.REST` to inject a mock client
# without going through the alpaca SDK's import machinery.
from alpaca_trade_api.rest import REST

import config
from equity_history import EquitySnapshotLog
from trade_log import TradeLog


# --------------------------------------------------------------------------- #
# Shared filtering                                                            #
# --------------------------------------------------------------------------- #
def _filter_rows(
    rows: List[dict],
    symbol: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> List[dict]:
    """Apply client-side filters on top of what TradeLog.all() supports."""
    def _parse(date_str: str) -> datetime:
        # Accept YYYY-MM-DD as shorthand for start-of-day UTC.
        return datetime.fromisoformat(date_str)

    since_dt = _parse(since) if since else None
    until_dt = _parse(until) if until else None
    result = []
    for r in rows:
        if symbol and r["symbol"] != symbol:
            continue
        exit_dt = datetime.fromisoformat(r["exit_time"])
        if since_dt and exit_dt < since_dt:
            continue
        if until_dt and exit_dt > until_dt:
            continue
        result.append(r)
    return result


# --------------------------------------------------------------------------- #
# Commands                                                                    #
# --------------------------------------------------------------------------- #
def cmd_summary(args) -> int:
    log = TradeLog(args.db)
    rows = _filter_rows(
        log.all(source=args.source, run_id=args.run_id),
        symbol=args.symbol, since=args.since, until=args.until,
    )

    if not rows:
        print("No trades match the filter.")
        return 0

    total = sum(r["pnl"] for r in rows)
    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] <= 0]
    print(f"Trades:    {len(rows)}")
    print(f"Wins:      {len(wins)}  ({len(wins) / len(rows):.1%})")
    print(f"Losses:    {len(losses)}")
    print(f"Net P&L:   ${total:+,.2f}")
    if wins:
        print(f"Avg win:   ${sum(r['pnl'] for r in wins) / len(wins):+,.2f}")
    if losses:
        print(f"Avg loss:  ${sum(r['pnl'] for r in losses) / len(losses):+,.2f}")

    # Per-symbol breakdown
    by_sym: dict[str, List[dict]] = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r)
    print("\nPer-symbol:")
    for sym, trades in sorted(by_sym.items()):
        sym_pnl = sum(t["pnl"] for t in trades)
        sym_wins = sum(1 for t in trades if t["pnl"] > 0)
        print(
            f"  {sym:5s}  {len(trades):3d} trades  "
            f"{sym_wins}/{len(trades)} wins  P&L: ${sym_pnl:+,.2f}"
        )
    return 0


def cmd_list(args) -> int:
    log = TradeLog(args.db)
    rows = _filter_rows(
        log.all(source=args.source, run_id=args.run_id),
        symbol=args.symbol, since=args.since, until=args.until,
    )
    if args.limit:
        rows = rows[-args.limit:]

    if not rows:
        print("No trades match the filter.")
        return 0

    header = f"{'Entry':<20} {'Exit':<20} {'Sym':<6} {'Qty':>6} {'Entry':>8} {'Exit':>8} {'P&L':>10}  Reason"
    print(header)
    print("-" * len(header))
    for r in rows:
        entry = r["entry_time"][:19]
        exit_ = r["exit_time"][:19]
        print(
            f"{entry:<20} {exit_:<20} {r['symbol']:<6} "
            f"{r['qty']:>6.0f} {r['entry_price']:>8.2f} "
            f"{r['exit_price']:>8.2f} {r['pnl']:>+10.2f}  {r['reason']}"
        )
    return 0


def _read_open_position_symbols(heartbeat_path: str) -> List[str]:
    """Return the list of currently-held symbols from heartbeat.json's
    `open_positions` map. Returns [] for any failure mode (missing file,
    malformed JSON, missing key) — equity-history's contract is that
    `positions` always exists, even when it's empty.
    """
    try:
        with open(heartbeat_path) as f:
            hb = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    open_positions = hb.get("open_positions") or {}
    return sorted(open_positions.keys())


def _fetch_position_symbols_from_alpaca() -> List[str]:
    """Pull the live list of held symbols from Alpaca. Raises on any
    failure (auth, network, missing creds) — caller catches to fall back
    to the heartbeat file.
    """
    api = REST(
        key_id=config.API_KEY,
        secret_key=config.API_SECRET,
        base_url=config.BASE_URL,
    )
    return sorted(p.symbol for p in api.list_positions())


def _resolve_position_symbols(heartbeat_path: str) -> List[str]:
    """Resolve the currently-held symbol list, preferring Alpaca live state
    over the heartbeat file. Order:

      1. Try Alpaca's `list_positions()` — source of truth.
      2. On any failure, fall back to `heartbeat.json`'s open_positions map.
      3. If both fail, return [].

    The heartbeat fallback exists because the bot might be running on a
    machine without Alpaca creds (e.g., a read-only reporting host) — the
    heartbeat is a best-effort cached snapshot in that case.
    """
    try:
        return _fetch_position_symbols_from_alpaca()
    except Exception:
        return _read_open_position_symbols(heartbeat_path)


def cmd_equity_history(args) -> int:
    """Dump the recorded equity series as JSON for downstream consumers
    (e.g. a website-side chart). Output shape:

        {
          "start_date": "2026-04-22",      # earliest snapshot for this sleeve
          "as_of": "2026-05-03T...",       # timestamp of the newest snapshot
          "snapshots": [{"date": ..., "equity": ...}, ...],  # oldest → newest
          "positions": ["AAPL", "MSFT", ...]                 # currently held
        }

    --days N            → N most recent calendar days (includes weekends)
    --trading-days N    → N most recent weekdays (Sat/Sun excluded)
    """
    log = EquitySnapshotLog(args.db)
    if args.trading_days is not None:
        snapshots = log.recent(
            days=args.trading_days, sleeve=args.sleeve, weekday_only=True,
        )
    else:
        snapshots = log.recent(days=args.days, sleeve=args.sleeve)

    payload = {
        "start_date": log.start_date(sleeve=args.sleeve),
        "as_of": (
            snapshots[-1].timestamp.astimezone(timezone.utc).isoformat()
            if snapshots else None
        ),
        "snapshots": [
            {"date": s.timestamp.astimezone(timezone.utc).date().isoformat(),
             "equity": s.equity}
            for s in snapshots
        ],
        "positions": _resolve_position_symbols(args.heartbeat),
    }

    text = json.dumps(payload, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(text)
    else:
        print(text)
    return 0


def cmd_export(args) -> int:
    log = TradeLog(args.db)
    rows = _filter_rows(
        log.all(source=args.source, run_id=args.run_id),
        symbol=args.symbol, since=args.since, until=args.until,
    )
    if not rows:
        print("No trades match the filter.")
        return 0

    fieldnames = list(rows[0].keys())
    with open(args.path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Exported {len(rows)} trades → {args.path}")
    return 0


# --------------------------------------------------------------------------- #
# Arg parsing                                                                 #
# --------------------------------------------------------------------------- #
def _add_filters(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--db", default=getattr(config, "TRADE_LOG_DB", "trades.db"),
                    help="Path to the SQLite trade log")
    sp.add_argument("--symbol", help="Only include this symbol")
    sp.add_argument("--source", help="Filter by source ('live', 'backtest')")
    sp.add_argument("--run-id", dest="run_id", help="Filter by backtest run_id")
    sp.add_argument("--since", help="Include only trades with exit_time ≥ this "
                                    "(ISO-8601, e.g. 2024-01-01)")
    sp.add_argument("--until", help="Include only trades with exit_time ≤ this")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Query the trade log.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("summary", help="Aggregate stats")
    _add_filters(sp)
    sp.set_defaults(func=cmd_summary)

    sp = sub.add_parser("list", help="Print individual trades")
    _add_filters(sp)
    sp.add_argument("--limit", type=int, help="Show only the last N trades")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("export", help="Write filtered trades to CSV")
    sp.add_argument("path", help="Destination CSV path")
    _add_filters(sp)
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser(
        "equity-history",
        help="Dump recorded daily equity snapshots as JSON",
    )
    sp.add_argument("--db", default=getattr(config, "TRADE_LOG_DB", "trades.db"),
                    help="Path to the SQLite trade log")
    # --days and --trading-days are mutually exclusive: callers pick one
    # rather than relying on a silent precedence rule.
    window = sp.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, default=7,
                        help="Number of most recent calendar days (default 7)")
    window.add_argument("--trading-days", dest="trading_days", type=int,
                        default=None,
                        help="Number of most recent trading days (Sat/Sun excluded)")
    sp.add_argument("--sleeve", default="total",
                    help="Sleeve to dump: 'total' (default), 'ema', 'dm'")
    sp.add_argument("--heartbeat",
                    default=getattr(config, "HEARTBEAT_FILE", "heartbeat.json"),
                    help="Path to heartbeat.json (for current positions list)")
    sp.add_argument("--output", help="Write JSON to this file (defaults to stdout)")
    sp.set_defaults(func=cmd_equity_history)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
