"""Apply the risk engine's in-trade rules (strategy/trade_management.py) to
EXISTING open positions: move stops to breakeven, trail stops once a
position is running, and flag partial-profit-taking opportunities.

Run ON YOUR MACHINE while TWS is open and logged in:

    python examples/manage_open_positions.py

For every open long stock position with a resting stop order, this
computes the R-multiple move since entry and checks it against
strategy.config's breakeven/trailing/partial-profit thresholds. Nothing is
submitted without an explicit typed YES per action -- same pattern as
protect_positions.py. A "replace stop" is done as cancel-then-place; there
is no in-place order modification here by design, so the old stop is only
cancelled once the new one is confirmed.

Positions with no resting stop are skipped with a warning (run
protect_positions.py first).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import LimitOrder, StopOrder

from ibkr.client import IBKRClient
from ibkr.data import fetch_daily_bars
from strategy.config import DEFAULT_CONFIG
from strategy.indicators import atr as compute_atr
from strategy.signals import compute_indicator_frame
from strategy.trade_management import (
    OpenPosition,
    breakeven_stop,
    partial_profit_plan,
    r_multiple,
    trailing_stop,
)


def main() -> None:
    client = IBKRClient()
    ib = client.connect(retries=5)
    print(f"Connected. Accounts: {ib.managedAccounts()}")

    positions = [p for p in ib.positions() if p.position > 0 and p.contract.secType == "STK"]
    if not positions:
        print("No long stock positions found.")
        client.disconnect()
        return

    stop_trades = {
        t.contract.symbol: t
        for t in ib.openTrades()
        if t.order.action == "SELL" and t.order.orderType == "STP"
    }

    for pos in positions:
        symbol = pos.contract.symbol
        qty = float(pos.position)
        avg_cost = float(pos.avgCost)

        stop_trade = stop_trades.get(symbol)
        if stop_trade is None:
            print(f"\n{symbol}: no resting STP order found, skipping "
                  f"(run protect_positions.py first)")
            continue
        current_stop = float(stop_trade.order.auxPrice)

        [ticker] = ib.reqTickers(pos.contract)
        current_price = ticker.last if ticker.last and ticker.last > 0 else ticker.marketPrice()
        if not current_price or current_price <= 0:
            print(f"\n{symbol}: no live quote, skipping")
            continue

        df = fetch_daily_bars(ib, symbol, duration="6 M")
        indf = compute_indicator_frame(df, DEFAULT_CONFIG)
        current_atr = float(indf["atr"].iloc[-1])

        open_pos = OpenPosition(
            side="BUY", entry=avg_cost, stop=current_stop, quantity=qty, atr_at_entry=current_atr,
        )
        r = r_multiple(open_pos, current_price)
        print(f"\n{symbol}: {qty} sh @ avg {avg_cost:.2f}, now {current_price:.2f} "
              f"({r:+.2f}R), current stop {current_stop:.2f}")

        new_be = breakeven_stop(open_pos, current_price, DEFAULT_CONFIG)
        new_trail = trailing_stop(open_pos, current_price, current_atr, DEFAULT_CONFIG)
        # trailing_stop already only returns a level tighter than the current
        # stop; prefer it over a plain breakeven move when both trigger.
        new_stop = new_trail or new_be

        if new_stop is not None:
            label = "trailing stop" if new_stop == new_trail else "breakeven stop"
            print(f"  {label}: move stop {current_stop:.2f} -> {new_stop:.2f}")
            answer = input(f"  Type YES to replace the {symbol} stop: ").strip().upper()
            if answer in ("YES", "Y"):
                new_order = StopOrder("SELL", qty, round(new_stop, 2), tif="GTC")
                ib.placeOrder(pos.contract, new_order)
                ib.sleep(1)
                ib.cancelOrder(stop_trade.order)
                print(f"  {symbol} stop replaced: {current_stop:.2f} -> {new_stop:.2f}")
            else:
                print(f"  {symbol} stop unchanged.")
        else:
            print(f"  no stop change triggered yet "
                  f"(breakeven at {DEFAULT_CONFIG.breakeven_at_r}R, "
                  f"trailing activates at {DEFAULT_CONFIG.trailing_stop_activate_r}R)")

        plan = partial_profit_plan(open_pos, current_price, DEFAULT_CONFIG)
        if plan is not None:
            print(f"  partial profit triggered ({DEFAULT_CONFIG.partial_profit_r}R): "
                  f"sell {plan.sell_quantity:.4g} of {qty}, keep {plan.remaining_quantity:.4g} running")
            answer = input(f"  Type YES to sell {plan.sell_quantity:.4g} {symbol} now: ").strip().upper()
            if answer in ("YES", "Y"):
                order = LimitOrder("SELL", plan.sell_quantity, round(current_price * 0.999, 2))
                ib.placeOrder(pos.contract, order)
                ib.sleep(1)
                print(f"  {symbol} partial-profit sell submitted.")
            else:
                print(f"  {symbol} partial profit skipped.")

    client.disconnect()


if __name__ == "__main__":
    main()
