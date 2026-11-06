"""Populate a daily-bar cache under data/ from Kraken's public OHLC endpoint.

Writes the same `data/<cache>/PAIR.json` format as fetch_bar_cache.py, so
`ibkr.cache.load_cache` reads it unchanged. No API key needed.

Run ON YOUR MACHINE (api.kraken.com is blocked from the Claude cloud
container):

    python examples/fetch_kraken_cache.py kraken1d
    python examples/fetch_kraken_cache.py kraken1d XBTUSD ETHUSD SOLUSD --refetch

Limits worth knowing before reading anything into the data:
- Kraken returns at most the 720 most recent bars, so ~2 years of dailies.
- Daily bars are UTC days (00:00 UTC = 16:00/17:00 Pacific), not US
  sessions, and crypto trades 7 days a week -- ~365 bars a year, not ~252.
  Anything that annualizes by 252 will be wrong on this cache.
- The still-forming bar for today is dropped (see kraken.desk).

Any strategy tested on this data goes through net_of_commission.py with
Kraken's own fee schedule before it is described as working -- the same
rule as everything else in CLAUDE.md.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibkr.cache import cached_symbols, save_bars
from kraken.client import KrakenClient, KrakenError
from kraken.desk import DEFAULT_WATCHLIST, daily_bars
from paths import bar_cache_dir


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cache", help="cache directory name under data/, e.g. kraken1d")
    p.add_argument("pairs", nargs="*", help=f"pairs (default: {' '.join(DEFAULT_WATCHLIST)})")
    p.add_argument("--refetch", action="store_true", help="re-fetch pairs already cached")
    p.add_argument("--pace", type=float, default=1.0, help="seconds between requests (default: 1.0)")
    args = p.parse_args()

    cache = bar_cache_dir(args.cache, create=True)
    pairs = [x.upper() for x in args.pairs] or list(DEFAULT_WATCHLIST)
    done = set(cached_symbols(cache))
    todo = pairs if args.refetch else [x for x in pairs if x not in done]
    print(f"{len(todo)} to fetch into {cache} ({len(pairs) - len(todo)} already cached)")

    client = KrakenClient()  # public data only; no key read
    failed = []
    for i, pair in enumerate(todo):
        try:
            key, frame = daily_bars(client, pair)
            if frame.empty:
                raise KrakenError("no completed bars returned")
            # Filed under the name the user typed (XBTUSD), not Kraken's
            # internal key (XXBTZUSD), so the file is findable.
            save_bars(frame, pair, cache)
            print(f"  {pair:<10} {len(frame):>4} bars  {frame.index[0]:%Y-%m-%d} -> {frame.index[-1]:%Y-%m-%d}  ({key})")
        except (KrakenError, ValueError) as exc:
            failed.append(pair)
            print(f"  {pair:<10} FAILED: {exc}")
        if i < len(todo) - 1:
            time.sleep(args.pace)

    if failed:
        raise SystemExit(f"{len(failed)} failed: {' '.join(failed)} -- rerun to retry just those")


if __name__ == "__main__":
    main()
