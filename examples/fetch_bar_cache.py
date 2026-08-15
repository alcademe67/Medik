"""Populate a daily-bar cache under data/ from TWS.

The backtests read from `data/<cache>/SYMBOL.json`. Before this script the
caches were produced ad-hoc by agents fetching into a session scratchpad,
which meant the evidence behind the adopted strategy evaporated when the
container did. This makes them reproducible.

Run ON YOUR MACHINE while TWS is open and logged in:

    python examples/fetch_bar_cache.py data5y --etfs --duration "5 Y"
    python examples/fetch_bar_cache.py data2y --universe
    python examples/fetch_bar_cache.py data2y NVDA AMD PLTR

Resumable by design: symbols already cached are skipped unless --refetch.
A full universe fetch takes a while and IBKR will throttle it, so expect to
run it more than once — rerunning costs nothing and fills only the gaps.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibkr.cache import CacheError, cached_symbols, load_bars, save_bars
from ibkr.client import IBKRClient
from ibkr.data import fetch_daily_bars
from paths import bar_cache_dir
from strategy.core_holdings import CORE_ETFS
from strategy.universe import LIQUID_UNIVERSE

# What backtest/run_lowfreq_comparison.py actually asks for: the risk assets
# it rotates between plus the bond sleeve it can sit in.
DEFAULT_ETFS = ("SPY", "QQQ", "IWM", "EFA", "TLT")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cache", help="cache directory name under data/, e.g. data2y")
    p.add_argument("symbols", nargs="*", help="symbols to fetch (default: --universe)")
    p.add_argument("--universe", action="store_true", help=f"fetch strategy.universe ({len(LIQUID_UNIVERSE)} names)")
    p.add_argument("--etfs", action="store_true", help=f"fetch the comparison ETFs {DEFAULT_ETFS}")
    p.add_argument("--core-etfs", action="store_true", help="fetch every whitelisted core ETF")
    p.add_argument("--duration", default="2 Y", help='IBKR duration string, e.g. "2 Y", "5 Y" (default: 2 Y)')
    p.add_argument("--refetch", action="store_true", help="re-fetch symbols already cached")
    p.add_argument(
        "--pace",
        type=float,
        default=1.5,
        help="seconds between requests (default: 1.5). IBKR allows ~60 historical "
             "requests per 10 minutes; raise this if you hit pacing violations.",
    )
    return p.parse_args()


def wanted_symbols(args: argparse.Namespace) -> list[str]:
    symbols: set[str] = {s.upper() for s in args.symbols}
    if args.universe:
        symbols |= set(LIQUID_UNIVERSE)
    if args.etfs:
        symbols |= set(DEFAULT_ETFS)
    if args.core_etfs:
        symbols |= set(CORE_ETFS)
    if not symbols:
        symbols = set(LIQUID_UNIVERSE)
    return sorted(symbols)


def main() -> None:
    args = parse_args()
    cache = bar_cache_dir(args.cache, create=True)
    symbols = wanted_symbols(args)

    already = set(cached_symbols(cache))
    if args.refetch:
        todo = symbols
    else:
        todo = [s for s in symbols if s not in already]
        # A cached-but-corrupt file must be retried, not counted as done --
        # that is exactly how the ragged LUNR/REGN/TMO files persisted.
        for symbol in (s for s in symbols if s in already):
            try:
                load_bars(symbol, cache)
            except CacheError as exc:
                print(f"  {symbol}: cached file is bad ({exc}) — refetching")
                todo.append(symbol)
        todo.sort()

    print(f"cache:    {cache}")
    print(f"duration: {args.duration}")
    print(f"symbols:  {len(todo)} to fetch, {len(symbols) - len(todo)} already cached\n")
    if not todo:
        print("nothing to do.")
        return

    ok, failed = 0, []
    with IBKRClient() as client:
        ib = client.ib
        for i, symbol in enumerate(todo, 1):
            try:
                df = fetch_daily_bars(ib, symbol, duration=args.duration)
                path = save_bars(df, symbol, cache)
                ok += 1
                print(f"[{i:>3}/{len(todo)}] {symbol:<6} {len(df):>5} bars  "
                      f"{df.index[0].date()}..{df.index[-1].date()}  -> {path.name}")
            except Exception as exc:  # noqa: BLE001 - log and keep going; rerun fills the gaps
                failed.append((symbol, str(exc)[:70]))
                print(f"[{i:>3}/{len(todo)}] {symbol:<6} FAILED: {str(exc)[:70]}")
            if i < len(todo):
                ib.sleep(args.pace)  # event-loop-friendly; time.sleep would stall ib_async

    print(f"\nfetched {ok}, failed {len(failed)}")
    for symbol, why in failed:
        print(f"  {symbol:<6} {why}")
    if failed:
        print("\nRerun the same command to retry just these — cached symbols are skipped.")


if __name__ == "__main__":
    main()
