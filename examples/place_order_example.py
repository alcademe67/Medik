"""Example of submitting a live limit order through TWS.

This sends a REAL order to your live account when confirm=True. Read the
order details below and edit them before running — the default price is
deliberately unrealistic so the order won't fill if you forget to change it.

Usage:
    python examples/place_order_example.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibkr.client import IBKRClient
from ibkr.orders import place_limit_order


def main() -> None:
    with IBKRClient() as client:
        trade = place_limit_order(
            client.ib,
            symbol="AAPL",
            action="BUY",
            quantity=1,
            limit_price=1.00,  # edit before using — this is far below market on purpose
            confirm=True,
        )
        print(f"Order status: {trade.orderStatus.status}")


if __name__ == "__main__":
    main()
