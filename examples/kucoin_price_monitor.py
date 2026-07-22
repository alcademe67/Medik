"""Poll a KuCoin ticker and announce threshold crossings. Read-only.

Run from the repository root, e.g.:

    python -m examples.kucoin_price_monitor --symbol BTC-USDT --above 120000 --below 100000
"""

import argparse
import time
from datetime import datetime, timezone

from examples._env import load_env
from kucoin.client import KuCoinClient
from kucoin.data import last_price


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTC-USDT", help="market symbol, e.g. BTC-USDT")
    parser.add_argument("--above", type=float, default=None, help="alert when price rises above this")
    parser.add_argument("--below", type=float, default=None, help="alert when price falls below this")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between polls")
    args = parser.parse_args()

    load_env()
    client = KuCoinClient()

    print(f"Monitoring {args.symbol} every {args.interval:g}s (Ctrl-C to stop)")
    was_above = None
    was_below = None
    while True:
        price = last_price(client, args.symbol)
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"[{stamp}] {args.symbol} {price}"

        if args.above is not None:
            above = price > args.above
            if above and was_above is False:
                line += f"  *** crossed ABOVE {args.above} ***"
            was_above = above
        if args.below is not None:
            below = price < args.below
            if below and was_below is False:
                line += f"  *** crossed BELOW {args.below} ***"
            was_below = below

        print(line, flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
