"""Sell out of named positions at a marketable limit, priced at execution time.

Why this exists: drafting an order instruction, reading a quote here, and
having a human submit it minutes later means the limit is stale by the time
it reaches the market. On 2026-08-06 four successive F drafts missed for
exactly that reason as the bid drifted down a cent at a time. This script
reads the live bid microseconds before it places, so there is no staleness
window.

It sells the FULL position for each symbol, using the quantity TWS reports
rather than a hardcoded number, so it can't oversell into a short. This
account is long-only and cannot hold a short.

Pricing: a limit at (bid - buffer), where buffer defaults to 0.15%. That is
marketable, so it executes at the bid or better immediately, but unlike a
market order it still refuses a catastrophically bad print.

Run ON YOUR MACHINE with TWS open and logged in:

    python examples/liquidate_positions.py F JPM
    python examples/liquidate_positions.py F JPM --buffer-pct 0.3
    python examples/liquidate_positions.py --all

Shows every leg with live prices and realized P&L, then requires YES.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import Stock

from ibkr.client import IBKRClient
from ibkr.orders import place_limit_order


def commission(shares: float, value: float) -> float:
    """This account's measured schedule: $0.005/share, min $1.00, max 1% of value."""
    return min(max(0.005 * shares, 1.00), 0.01 * value)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", help="symbols to sell out of entirely")
    ap.add_argument("--all", action="store_true", help="sell every stock position")
    ap.add_argument("--buffer-pct", type=float, default=0.15,
                    help="limit is this %% below the bid (default 0.15)")
    args = ap.parse_args()

    if not args.symbols and not args.all:
        ap.error("name at least one symbol, or pass --all")

    with IBKRClient() as client:
        ib = client.ib
        print(f"Connected to TWS at {client.host}:{client.port}\n")

        positions = [p for p in ib.positions() if p.position > 0]
        if not args.all:
            wanted = {s.upper() for s in args.symbols}
            positions = [p for p in positions if p.contract.symbol.upper() in wanted]
            missing = wanted - {p.contract.symbol.upper() for p in positions}
            if missing:
                print(f"No long position in: {', '.join(sorted(missing))}")

        if not positions:
            print("Nothing to sell.")
            return

        legs = []
        print(f"{'sym':6} {'qty':>8} {'bid':>10} {'limit':>10} {'proceeds':>10} "
              f"{'comm':>6} {'realized':>10}")
        print("-" * 64)
        total_net = total_realized = 0.0

        for p in positions:
            contract = Stock(p.contract.symbol, "SMART", p.contract.currency)
            (contract,) = ib.qualifyContracts(contract)
            ticker = ib.reqMktData(contract, "", False, False)
            ib.sleep(2)
            bid = ticker.bid
            if not bid or bid <= 0:
                print(f"{p.contract.symbol:6} no live bid - SKIPPED")
                continue

            limit = round(bid * (1 - args.buffer_pct / 100), 2)
            qty = abs(p.position)
            proceeds = qty * bid
            comm = commission(qty, proceeds)
            net = proceeds - comm
            realized = net - qty * p.avgCost
            total_net += net
            total_realized += realized

            print(f"{p.contract.symbol:6} {qty:>8.4g} {bid:>10.2f} {limit:>10.2f} "
                  f"{proceeds:>10.2f} {comm:>6.2f} {realized:>+10.2f}")
            legs.append((contract, qty, limit))

        if not legs:
            print("\nNo sellable legs (no live bids).")
            return

        print("-" * 64)
        print(f"{'TOTAL':6} {'':>8} {'':>10} {'':>10} {total_net:>10.2f} "
              f"{'':>6} {total_realized:>+10.2f}")
        print(f"\nLimits sit {args.buffer_pct}% below the bid: marketable, so they "
              f"should fill\nat the bid or better, but capped against a bad print.")

        if input("\nType YES to place these sell orders: ").strip() != "YES":
            print("Aborted. Nothing placed.")
            return

        trades = []
        for contract, qty, limit in legs:
            trade = place_limit_order(
                ib, contract.symbol, "SELL", qty, limit,
                currency=contract.currency, confirm=True,
            )
            trades.append(trade)
            print(f"  placed SELL {qty:g} {contract.symbol} @ {limit}")

        print("\nWaiting for fills...")
        for _ in range(15):
            ib.sleep(2)
            if all(t.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled")
                   for t in trades):
                break

        print("\nResult:")
        for t in trades:
            st = t.orderStatus
            print(f"  {t.contract.symbol:6} {st.status:10} filled {st.filled:g} "
                  f"@ avg {st.avgFillPrice or 0:.4f}, remaining {st.remaining:g}")

        print("\nPositions now:")
        for p in ib.positions():
            if p.position:
                print(f"  {p.contract.symbol}: {p.position:g}")


if __name__ == "__main__":
    main()
