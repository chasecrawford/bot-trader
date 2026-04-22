"""
smoke_test.py — Verify Alpaca credentials and connectivity.

Run this once after setting up .env to confirm:
  1. python-dotenv loaded your .env file
  2. ALPACA_API_KEY / ALPACA_API_SECRET / ALPACA_BASE_URL are all set
  3. Alpaca accepts the credentials (the real test — a 401 here means bad keys)
  4. The base URL points at paper (safety check before we ever run trader.py)

Usage:
    python smoke_test.py
"""

from __future__ import annotations

import sys

try:
    from alpaca_trade_api.rest import REST, APIError
except ImportError:
    print(
        "Missing dependency: pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise

import config


def _mask(value: str, keep: int = 4) -> str:
    """Show first `keep` chars, mask the rest. For safe printing of IDs."""
    if not value or len(value) <= keep:
        return "****"
    return value[:keep] + "*" * (len(value) - keep)


def main() -> int:
    # Step 1: credentials loaded at all?
    if "YOUR_API_KEY" in config.API_KEY or "YOUR_API_SECRET" in config.API_SECRET:
        print(
            "✗ Credentials not loaded. Check that .env contains ALPACA_API_KEY "
            "and ALPACA_API_SECRET (not the APCA_* names), and that "
            "python-dotenv is installed.",
            file=sys.stderr,
        )
        return 1

    # Step 2: safety — refuse to run against the live endpoint from a smoke test.
    if "paper" not in config.BASE_URL:
        print(
            f"✗ BASE_URL does not look like a paper endpoint: {config.BASE_URL}\n"
            f"  Refusing to run smoke test against live trading.",
            file=sys.stderr,
        )
        return 1

    print(f"→ Connecting to {config.BASE_URL}")
    print(f"  Key ID: {_mask(config.API_KEY)}")

    api = REST(
        key_id=config.API_KEY,
        secret_key=config.API_SECRET,
        base_url=config.BASE_URL,
    )

    # Step 3: the real auth test.
    try:
        account = api.get_account()
    except APIError as exc:
        print(f"✗ Alpaca API rejected the request: {exc}", file=sys.stderr)
        print("  Most common cause: wrong key/secret, or key is for the other "
              "environment (live vs. paper).", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"✗ Network or unknown error: {exc}", file=sys.stderr)
        return 1

    # Step 4: print a readable account snapshot.
    print("✓ Authenticated.")
    print(f"  Account:        {_mask(account.account_number, keep=3)}")
    print(f"  Status:         {account.status}")
    print(f"  Equity:         ${float(account.equity):,.2f}")
    print(f"  Cash:           ${float(account.cash):,.2f}")
    print(f"  Buying power:   ${float(account.buying_power):,.2f}")
    print(f"  Pattern day trader: {account.pattern_day_trader}")

    # Step 5: also confirm market clock endpoint works — trader.py uses it
    # to decide whether to run each tick.
    try:
        clock = api.get_clock()
        print(f"  Market open now: {clock.is_open}")
        print(f"  Next open:       {clock.next_open}")
        print(f"  Next close:      {clock.next_close}")
    except Exception as exc:
        print(f"⚠ Account OK but clock endpoint failed: {exc}", file=sys.stderr)
        # Non-fatal — auth works, which is the main thing.

    print("\nAll good. You can now run: python trader.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
