"""Attach protective stop + take-profit brackets to EXISTING positions.

Run ON YOUR MACHINE while TWS is open and logged in:

    python examples/protect_positions.py

For every open stock position it computes the plan's stop and 3R target
(anchored to your actual average cost), shows them, and on a typed YES
places a linked OCA pair (one-cancels-other): a GTC stop-loss and a GTC
take-profit. Whichever fills first automatically cancels the other, so
there is no double-sell risk. Positions that already have working sell
orders are skipped.

Stop distances per symbol come from the signal's 2xATR at scan time;
symbols not listed fall back to 8% of average cost.
"""
from __future__ import annotations

from ib_async import LimitOrder, Order, StopOrder

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibkr.client import IBKRClient
from strategy.config import DEFAULT_CONFIG

STOP_DISTANCES = {
    "RIOT": 3.95,
    "NVDA": 14.70,
    "XOM": 6.65,
}
FALLBACK_STOP_PCT = 0.08


def main() -> None:
    client = IBKRClient()
    ib = client.connect(retries=5)
    print(f"Connected. Accounts: {ib.managedAccounts()}")

    open_sell_symbols = {
        t.contract.symbol for t in ib.openTrades() if t.order.action == "SELL"
    }

    positions = [p for p in ib.positions() if p.position > 0 and p.contract.secType == "STK"]
    if not positions:
        print("No long stock positions found — nothing to protect.")
        client.disconnect()
        return

    for pos in positions:
        symbol = pos.contract.symbol
        qty = float(pos.position)
        avg = float(pos.avgCost)
        if symbol in open_sell_symbols:
            print(f"{symbol}: already has a working sell order, skipping")
            continue

        dist = STOP_DISTANCES.get(symbol, round(avg * FALLBACK_STOP_PCT, 2))
        stop = round(avg - dist, 2)
        target = round(avg + DEFAULT_CONFIG.min_risk_reward * dist, 2)

        print(f"\n{symbol}: {qty} sh @ avg {avg:.2f}")
        print(f"  stop-loss  SELL {qty} STP {stop}  (GTC)")
        print(f"  take-profit SELL {qty} LMT {target}  (GTC)")
        answer = input(f"  Type YES to place this linked pair for {symbol}: ").strip().upper()
        if answer not in ("YES", "Y"):
            print(f"  {symbol} skipped.")
            continue

        contract = pos.contract
        contract.exchange = "SMART"
        oca_group = f"protect_{symbol}"

        stop_order = StopOrder("SELL", qty, stop, tif="GTC")
        target_order = LimitOrder("SELL", qty, target, tif="GTC")
        for order in (stop_order, target_order):
            order.ocaGroup = oca_group
            order.ocaType = 1  # cancel remaining orders in group on fill
            ib.placeOrder(contract, order)
        ib.sleep(2)
        print(f"  {symbol} protected: stop {stop} / target {target}, linked OCA.")

    print("\nWorking orders now:")
    for t in ib.openTrades():
        print(f"  {t.contract.symbol}: {t.order.action} {t.order.totalQuantity} "
              f"{t.order.orderType} [{t.orderStatus.status}]")
    client.disconnect()


if __name__ == "__main__":
    main()
