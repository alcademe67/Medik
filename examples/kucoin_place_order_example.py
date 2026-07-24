"""Example: place a small resting limit buy order on KuCoin spot.

Safety:
- Dry-run by default; it only submits when the LIVE environment variable
  is set to "1".
- kucoin.orders refuses to submit any order without confirm=True.

Run from the repository root:

    python -m examples.kucoin_place_order_example          # dry run
    LIVE=1 python -m examples.kucoin_place_order_example   # real order
"""

import os

from examples._env import load_env
from kucoin.client import KuCoinClient
from kucoin.data import last_price
from kucoin.orders import open_orders, place_limit_order

SYMBOL = "BTC-USDT"
SIZE = "0.0001"
DISCOUNT = 0.95  # bid 5% below market so the order rests instead of filling


def main() -> None:
    load_env()
    client = KuCoinClient()

    price = last_price(client, SYMBOL)
    bid = round(price * DISCOUNT, 1)
    print(f"{SYMBOL} last={price}; candidate order: limit buy {SIZE} @ {bid}")

    if os.getenv("LIVE") != "1":
        print("Dry run - no order sent. Set LIVE=1 to place it for real.")
        return

    order = place_limit_order(client, SYMBOL, "buy", size=SIZE, price=str(bid), confirm=True)
    print(f"Order placed: {order}")
    print(f"Open orders on {SYMBOL}: {open_orders(client, SYMBOL)}")


if __name__ == "__main__":
    main()
