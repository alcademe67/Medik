"""Print a KuCoin balance summary with an estimated total USDT value.

Read-only: this never places or cancels orders. It lists every account
with a non-zero balance and estimates each asset's value by pulling its
CURRENCY-USDT ticker, then prints a portfolio total.

Run from the repository root:

    python -m examples.kucoin_balance
"""

import sys

from examples._env import load_env
from kucoin.client import KuCoinAPIError, KuCoinClient
from kucoin.portfolio import summarize


def make_price_lookup(client: KuCoinClient):
    """Return a cached currency -> USDT price function backed by the client."""
    cache: dict = {}

    def lookup(currency: str):
        if currency not in cache:
            try:
                cache[currency] = float(client.ticker(f"{currency}-USDT")["price"])
            except (KuCoinAPIError, KeyError, TypeError, ValueError):
                cache[currency] = None
        return cache[currency]

    return lookup


def main() -> int:
    load_env()
    client = KuCoinClient()

    if not client.has_credentials:
        print(
            "No API credentials configured. Set KUCOIN_API_KEY, KUCOIN_API_SECRET "
            "and KUCOIN_API_PASSPHRASE (copy .env.example to .env), then re-run."
        )
        return 1

    accounts = client.accounts()
    summary = summarize(accounts, make_price_lookup(client))

    if not summary["rows"]:
        print("No non-zero balances found.")
        return 0

    print(f"{'CURRENCY':<10}{'BALANCE':>20}{'AVAILABLE':>20}{'~USDT VALUE':>18}")
    print("-" * 68)
    for row in summary["rows"]:
        value = "-" if row["usdt_value"] is None else f"{row['usdt_value']:,.2f}"
        print(
            f"{row['currency']:<10}{row['balance']:>20.8f}"
            f"{row['available']:>20.8f}{value:>18}"
        )
    print("-" * 68)
    print(f"{'TOTAL':<10}{'':>40}{summary['total_usdt']:>18,.2f}  USDT")
    if summary["unpriced"]:
        print(
            f"(no USDT market for: {', '.join(summary['unpriced'])} - "
            "excluded from total)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
