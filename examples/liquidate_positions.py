"""Sell out of named positions at a marketable limit, priced at execution time.

Why this exists: drafting an order instruction, reading a quote elsewhere, and
having a human submit it minutes later means the limit is stale by the time it
reaches the market. On 2026-08-06 several successive F sell drafts missed for
exactly that reason as the bid drifted down a cent at a time. This reads the
live bid microseconds before it places, so there is no staleness window.

It sells the FULL position for each symbol using the quantity and the
contract TWS reports, so it cannot oversell into a short and cannot resolve
to the wrong listing. This account is long-only and cannot hold a short.

Pricing: a limit at (bid - buffer), default 1%. That is marketable, so it
executes at the prevailing bid or better immediately -- the limit is a FLOOR,
not the sale price -- but unlike a market order it still refuses a
catastrophically bad print. If no live bid is available it falls back to
last, then previous close, and says which it used. --price overrides
everything if market data is unavailable entirely.

Run ON YOUR MACHINE with TWS open and logged in:

    python examples/liquidate_positions.py F JPM --dry-run   # diagnose, place nothing
    python examples/liquidate_positions.py F JPM
    python examples/liquidate_positions.py F JPM --price F=13.90
    python examples/liquidate_positions.py --all

Every step prints what it is doing, so a failure says which step failed.
"""

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibkr.client import IBKRClient
from ibkr.orders import place_limit_order_on_contract

# 1=live, 2=frozen, 3=delayed, 4=delayed-frozen. Tried in this order: an
# account without a real-time subscription for a name returns NaN on type 1,
# which silently poisoned the limit price in the first version of this script.
MKT_DATA_TYPES = [(1, "live"), (2, "frozen"), (3, "delayed"), (4, "delayed-frozen")]


def _ok(x) -> bool:
    """IBKR returns NaN or -1 for 'no data'. NaN is truthy, so test explicitly."""
    return x is not None and not math.isnan(x) and x > 0


def get_quote(ib, contract, wait: float = 3.0):
    """Return (price, label). Tries bid, then last, then close, escalating
    through market-data types until something real comes back."""
    for md_type, md_name in MKT_DATA_TYPES:
        ib.reqMarketDataType(md_type)
        ticker = ib.reqMktData(contract, "", False, False)
        ib.sleep(wait)
        for attr in ("bid", "last", "close"):
            val = getattr(ticker, attr, None)
            if _ok(val):
                ib.cancelMktData(contract)
                return float(val), f"{attr} ({md_name})"
        ib.cancelMktData(contract)
    return None, "no data"


def commission(shares: float, value: float) -> float:
    """This account's measured schedule: $0.005/share, min $1.00, max 1% of value."""
    return min(max(0.005 * shares, 1.00), 0.01 * value)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", help="symbols to sell out of entirely")
    ap.add_argument("--all", action="store_true", help="sell every long stock position")
    ap.add_argument("--buffer-pct", type=float, default=1.0,
                    help="limit is this %% below the reference price (default 1.0)")
    ap.add_argument("--price", action="append", default=[], metavar="SYM=PRICE",
                    help="override the reference price, e.g. --price F=13.90")
    ap.add_argument("--dry-run", action="store_true",
                    help="show everything, place nothing")
    args = ap.parse_args()

    if not args.symbols and not args.all:
        ap.error("name at least one symbol, or pass --all")

    overrides = {}
    for item in args.price:
        sym, _, val = item.partition("=")
        overrides[sym.strip().upper()] = float(val)

    with IBKRClient() as client:
        ib = client.ib
        print(f"[1/5] connected to TWS at {client.host}:{client.port}")

        # positions arrive asynchronously after connect; ask explicitly and
        # wait, or an immediate call can return an empty list.
        ib.reqPositions()
        ib.sleep(2)
        all_pos = [p for p in ib.positions() if p.position > 0]
        print(f"[2/5] {len(all_pos)} long position(s): "
              f"{', '.join(f'{p.contract.symbol} {p.position:g}' for p in all_pos) or '(none)'}")

        positions = all_pos
        if not args.all:
            wanted = {s.upper() for s in args.symbols}
            positions = [p for p in all_pos if p.contract.symbol.upper() in wanted]
            missing = wanted - {p.contract.symbol.upper() for p in positions}
            if missing:
                print(f"      no long position in: {', '.join(sorted(missing))}")

        if not positions:
            print("Nothing to sell.")
            return

        print(f"[3/5] pricing {len(positions)} leg(s)...")
        legs = []
        rows = []
        total_net = total_realized = 0.0

        for p in positions:
            sym = p.contract.symbol
            (contract,) = ib.qualifyContracts(p.contract)

            if sym.upper() in overrides:
                ref, label = overrides[sym.upper()], "manual override"
            else:
                ref, label = get_quote(ib, contract)

            if ref is None:
                print(f"      {sym}: NO PRICE ({label}) - skipped. "
                      f"Re-run with --price {sym}=<price> to place anyway.")
                continue

            limit = round(ref * (1 - args.buffer_pct / 100), 2)
            if limit <= 0:
                print(f"      {sym}: computed limit {limit} invalid - skipped")
                continue

            qty = abs(p.position)
            proceeds = qty * ref
            comm = commission(qty, proceeds)
            net = proceeds - comm
            realized = net - qty * p.avgCost
            total_net += net
            total_realized += realized
            rows.append((sym, qty, ref, label, limit, proceeds, comm, realized))
            legs.append((contract, qty, limit))

        if not legs:
            print("\nNo sellable legs - every leg failed to price. "
                  "Use --price SYM=<price> to bypass market data.")
            return

        print()
        print(f"{'sym':6} {'qty':>8} {'ref':>10} {'source':>18} {'limit':>9} "
              f"{'proceeds':>10} {'comm':>6} {'realized':>10}")
        print("-" * 82)
        for sym, qty, ref, label, limit, proceeds, comm, realized in rows:
            print(f"{sym:6} {qty:>8.4g} {ref:>10.2f} {label:>18} {limit:>9.2f} "
                  f"{proceeds:>10.2f} {comm:>6.2f} {realized:>+10.2f}")
        print("-" * 82)
        print(f"{'TOTAL':6} {'':>8} {'':>10} {'':>18} {'':>9} {total_net:>10.2f} "
              f"{'':>6} {total_realized:>+10.2f}")
        print(f"\nLimits are {args.buffer_pct}% below the reference price. A sell limit "
              f"below the bid\nstill fills AT the bid - the limit is a floor, not the "
              f"sale price.")

        if args.dry_run:
            print("\n--dry-run: nothing placed.")
            return

        if input("\n[4/5] Type YES to place these sell orders: ").strip() != "YES":
            print("Aborted. Nothing placed.")
            return

        trades = []
        for contract, qty, limit in legs:
            try:
                trade = place_limit_order_on_contract(
                    ib, contract, "SELL", qty, limit, confirm=True
                )
                trades.append(trade)
                print(f"      placed SELL {qty:g} {contract.symbol} @ {limit}")
            except Exception as exc:
                print(f"      FAILED {contract.symbol}: {type(exc).__name__}: {exc}")

        if not trades:
            print("\nNothing was placed.")
            return

        print("[5/5] waiting for fills (up to 30s)...")
        for _ in range(15):
            ib.sleep(2)
            if all(t.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled")
                   for t in trades):
                break

        print("\nResult:")
        for t in trades:
            st = t.orderStatus
            print(f"  {t.contract.symbol:6} {st.status:12} filled {st.filled:g} "
                  f"@ avg {st.avgFillPrice or 0:.4f}, remaining {st.remaining:g}")
            for log in t.log[-3:]:
                if log.errorCode:
                    print(f"         err {log.errorCode}: {log.message}")

        ib.reqPositions()
        ib.sleep(2)
        print("\nPositions now:")
        remaining = [p for p in ib.positions() if p.position]
        for p in remaining:
            print(f"  {p.contract.symbol}: {p.position:g}")
        if not remaining:
            print("  (none)")


if __name__ == "__main__":
    main()
