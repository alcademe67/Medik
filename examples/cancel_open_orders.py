"""List live working orders and optionally cancel them.

The IBKR MCP connector has no cancel/modify endpoint, so orders left working
from a previous session (e.g. GTC stops) can only be cleared from the IBKR
app or from here, against a live TWS socket.

Dry-run by default: with no flags it only prints what is working. Cancelling
requires naming the order ids explicitly (or --all) AND typing YES at the
prompt, matching the confirm=True gate in ibkr/orders.py.

Prerequisites:
  - TWS open and logged into the target account, API socket enabled.

Usage:
    python examples/cancel_open_orders.py                    # list only
    python examples/cancel_open_orders.py --id 2086187473    # cancel one
    python examples/cancel_open_orders.py --id 1 --id 2      # cancel several
    python examples/cancel_open_orders.py --all              # cancel everything
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibkr.accounts import order_belongs_to, resolve_account
from ibkr.client import IBKRClient
from ibkr.orders import cancel_order


def describe(trade) -> str:
    o, c = trade.order, trade.contract
    bits = [f"{o.action} {o.totalQuantity} {c.symbol}", o.orderType]
    if getattr(o, "lmtPrice", 0):
        bits.append(f"LMT {o.lmtPrice}")
    if getattr(o, "auxPrice", 0):
        bits.append(f"STP {o.auxPrice}")
    bits.append(o.tif)
    return f"  [{o.orderId}] {' '.join(bits)}  status={trade.orderStatus.status}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, action="append", default=[],
                    help="order id to cancel (repeatable)")
    ap.add_argument("--all", action="store_true", help="cancel every working order")
    ap.add_argument("--account", default="",
                    help="account to cancel in; required when the login "
                         "manages more than one (or set IBKR_ACCOUNT)")
    args = ap.parse_args()

    with IBKRClient() as client:
        ib = client.ib
        print(f"Connected to TWS at {client.host}:{client.port}\n")

        account, why = resolve_account(list(ib.managedAccounts()),
                                       explicit=args.account)
        print(f"Account: {why}")
        if not account:
            print("\nREFUSING TO RUN — --all would reach into another "
                  "account's orders. Re-run with --account <id>.")
            return

        trades = [t for t in ib.openTrades()
                  if t.orderStatus.status not in
                  ("Filled", "Cancelled", "ApiCancelled", "Inactive")
                  and order_belongs_to(t, account)]

        if not trades:
            print("No working orders. Nothing to cancel.")
            return

        print(f"Working orders ({len(trades)}):")
        for t in trades:
            print(describe(t))

        if args.all:
            targets = trades
        elif args.id:
            wanted = set(args.id)
            targets = [t for t in trades if t.order.orderId in wanted]
            missing = wanted - {t.order.orderId for t in targets}
            if missing:
                print(f"\nNot found among working orders: {sorted(missing)}")
        else:
            print("\n(dry run - pass --id <orderId> or --all to cancel)")
            return

        if not targets:
            return

        print(f"\nAbout to CANCEL {len(targets)} order(s):")
        for t in targets:
            print(describe(t))

        if input('\nType YES to cancel these orders: ').strip() != "YES":
            print("Aborted. Nothing cancelled.")
            return

        for t in targets:
            cancel_order(ib, t)
            print(f"  cancel sent for [{t.order.orderId}]")

        ib.sleep(2)
        still = [t for t in ib.openTrades() if t.orderStatus.status not in
                 ("Filled", "Cancelled", "ApiCancelled", "Inactive")]
        print(f"\nWorking orders remaining: {len(still)}")
        for t in still:
            print(describe(t))


if __name__ == "__main__":
    main()
