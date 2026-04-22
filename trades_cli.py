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
import sys
from datetime import datetime
from typing import List, Optional

import config
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

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
