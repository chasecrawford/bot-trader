"""
status.py -- Quick at-a-glance status for the dual-strategy paper account.

Reads from Alpaca (current equity / positions) and from the local order log
(recent activity by sleeve). Doesn't touch any state. Safe to run any time.

Usage:
    python status.py            # default: 20 most-recent orders
    python status.py --orders 50
    python status.py --sleeve dm   # only show DM sleeve activity
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

try:
    from alpaca_trade_api.rest import REST
except ImportError:
    print("Missing dependency: pip install alpaca-trade-api", file=sys.stderr)
    raise

import config
from order_log import read_orders


def fetch_alpaca_state():
    api = REST(
        key_id=config.API_KEY,
        secret_key=config.API_SECRET,
        base_url=config.BASE_URL,
    )
    account = api.get_account()
    positions = api.list_positions()
    try:
        clock = api.get_clock()
        market_open = bool(clock.is_open)
    except Exception:
        market_open = None
    return {
        "endpoint": config.BASE_URL,
        "equity": float(account.equity),
        "cash": float(account.cash),
        "buying_power": float(account.buying_power),
        "status": account.status,
        "positions": positions,
        "market_open": market_open,
    }


def classify_position(symbol: str) -> str:
    """Return which sleeve a position belongs to ('ema', 'dm', or 'unknown')."""
    dm_set = set(config.DUAL_MOMENTUM_RISKY) | {config.DUAL_MOMENTUM_SAFE}
    if symbol in dm_set:
        return "dm"
    if symbol in set(config.WATCHLIST):
        return "ema"
    return "unknown"


def print_account(state: dict) -> None:
    print("=" * 70)
    print("ACCOUNT")
    print("=" * 70)
    print(f"Endpoint:       {state['endpoint']}")
    print(f"Status:         {state['status']}")
    open_str = (
        "OPEN" if state["market_open"] else
        "CLOSED" if state["market_open"] is False else
        "?"
    )
    print(f"Market:         {open_str}")
    print(f"Equity:         ${state['equity']:,.2f}")
    print(f"Cash:           ${state['cash']:,.2f}")
    print(f"Buying power:   ${state['buying_power']:,.2f}")

    ema_alloc = getattr(config, "ALLOCATION_EMA", 1.0)
    dm_alloc = getattr(config, "ALLOCATION_DM", 0.0)
    print(f"\nSleeve targets: EMA = {ema_alloc:.0%} (${state['equity'] * ema_alloc:,.0f}), "
          f"DM = {dm_alloc:.0%} (${state['equity'] * dm_alloc:,.0f})")


def print_positions(state: dict, sleeve_filter: str) -> None:
    print()
    print("=" * 70)
    print("POSITIONS")
    print("=" * 70)
    if not state["positions"]:
        print("(none)")
        return
    by_sleeve: dict = {"ema": [], "dm": [], "unknown": []}
    for p in state["positions"]:
        by_sleeve[classify_position(p.symbol)].append(p)
    for sleeve in ("dm", "ema", "unknown"):
        if sleeve_filter != "all" and sleeve_filter != sleeve:
            continue
        positions = by_sleeve[sleeve]
        if not positions:
            continue
        sleeve_value = sum(float(p.market_value) for p in positions)
        print(f"\n[{sleeve.upper()}] {len(positions)} positions, "
              f"market value ${sleeve_value:,.2f}")
        print(f"{'Symbol':<8}{'Qty':>12}{'Entry':>12}{'Current':>12}"
              f"{'P&L':>14}{'P&L%':>10}")
        for p in positions:
            qty = float(p.qty)
            entry = float(p.avg_entry_price)
            current = float(p.current_price)
            pnl = (current - entry) * qty
            pnl_pct = (current - entry) / entry * 100
            # Show 4 decimals if fractional, 0 otherwise
            qty_str = f"{qty:>12.4f}" if qty != int(qty) else f"{qty:>12.0f}"
            print(f"{p.symbol:<8}{qty_str}{entry:>12.2f}"
                  f"{current:>12.2f}{pnl:>+14.2f}{pnl_pct:>+9.2f}%")


def print_recent_orders(limit: int, sleeve_filter: str) -> None:
    print()
    print("=" * 70)
    print(f"RECENT ORDERS (last {limit})")
    print("=" * 70)
    rows = read_orders()
    if not rows:
        print("(no orders logged yet — logs/orders.csv is empty or missing)")
        return
    if sleeve_filter != "all":
        rows = [r for r in rows if r.get("sleeve") == sleeve_filter]
    rows = rows[-limit:]
    if not rows:
        print(f"(no orders for sleeve={sleeve_filter})")
        return
    print(f"{'Time (UTC)':<22}{'Sleeve':<7}{'Side':<6}{'Symbol':<8}"
          f"{'Qty':>8}{'Price':>10}  {'Status':<10}{'Reason'}")
    for r in rows:
        ts = r["timestamp_utc"][:19]  # drop tz suffix for compactness
        try:
            qty = f"{float(r['qty']):.0f}"
        except ValueError:
            qty = r["qty"]
        try:
            px = f"{float(r['price']):.2f}" if r["price"] else "-"
        except ValueError:
            px = r["price"]
        print(f"{ts:<22}{r['sleeve']:<7}{r['side']:<6}{r['symbol']:<8}"
              f"{qty:>8}{px:>10}  {r['status']:<10}{r['reason']}")

    # Summary by status
    counts = Counter(r["status"] for r in rows)
    print(f"\nStatus counts: {dict(counts)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Status overview for the bot-trader account.")
    parser.add_argument("--orders", type=int, default=20,
                        help="Number of recent orders to show (default 20)")
    parser.add_argument("--sleeve", choices=["all", "ema", "dm"], default="all",
                        help="Filter positions/orders to a specific sleeve")
    args = parser.parse_args()

    try:
        state = fetch_alpaca_state()
    except Exception as exc:
        print(f"Failed to fetch Alpaca state: {exc}", file=sys.stderr)
        return 1

    print_account(state)
    print_positions(state, args.sleeve)
    print_recent_orders(args.orders, args.sleeve)
    return 0


if __name__ == "__main__":
    sys.exit(main())
