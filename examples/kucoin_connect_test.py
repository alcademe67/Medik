"""Read-only KuCoin smoke test.

Prints server time and the BTC-USDT ticker, and lists non-zero account
balances when API credentials are configured. Never places orders.

Run from the repository root:

    python -m examples.kucoin_connect_test
"""

from examples._env import load_env
from kucoin.client import KuCoinClient
from kucoin.data import best_bid_ask, last_price


def main() -> None:
    load_env()
    client = KuCoinClient()

    print(f"Server time: {client.server_time()}")
    price = last_price(client, "BTC-USDT")
    bid, ask = best_bid_ask(client, "BTC-USDT")
    print(f"BTC-USDT last={price} bid={bid} ask={ask}")

    if not client.has_credentials:
        print("No API credentials configured - skipping private endpoints.")
        return

    print("Non-zero balances:")
    for account in client.accounts():
        if float(account.get("balance", 0) or 0) > 0:
            print(
                f"  {account['type']:>7} {account['currency']:>6} "
                f"balance={account['balance']} available={account['available']}"
            )


if __name__ == "__main__":
    main()
