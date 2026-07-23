"""Place today's gate-confirmed entries through the owner's own TWS.

Run this ON YOUR MACHINE while TWS (or IB Gateway) is open and logged in:

    python examples/place_today_orders.py

It shows each planned order with live prices and asks for a YES before
placing anything — that keystroke is the owner's explicit confirmation
required by this repo's execution policy. Each entry goes in as a bracket:
a Day limit buy at the live ask, plus an attached GTC stop-loss and a GTC
take-profit at 3R, so exits are managed automatically after the fill.

TWS live port is 7496 (default here). If you run IB Gateway instead of
TWS, set IBKR_PORT=4001 in .env.

Candidates below are from the 2026-07-23 scan. Edit or delete rows before
running on any other day — signals go stale.
"""
from __future__ import annotations

from ib_async import Stock

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibkr.client import IBKRClient
from strategy.config import DEFAULT_CONFIG
from strategy.risk import portfolio_headroom

# symbol, quantity, stop distance (from the signal's 2xATR), notes
CANDIDATES = [
    ("RIOT", 2.0, 3.95, "4/4 gate 2026-07-23"),
    ("NVDA", 0.30, 14.70, "4/4 gate 2026-07-23"),
    ("XOM", 0.40, 6.65, "4/4 gate 2026-07-23; holds through Jul 31 earnings"),
]


def main() -> None:
    client = IBKRClient()  # 127.0.0.1:7496 unless .env overrides
    ib = client.connect(retries=5)
    accounts = ib.managedAccounts()
    print(f"Connected to TWS. Accounts: {accounts}")
    if any(a.startswith("D") for a in accounts):
        print("NOTE: this looks like a PAPER account — orders will be paper trades.")
    else:
        print("*** LIVE ACCOUNT — these orders use real money. ***")

    available = net_liq = gross = None
    for row in ib.accountSummary():
        if row.tag == "AvailableFunds":
            available = float(row.value)
        elif row.tag == "NetLiquidation":
            net_liq = float(row.value)
        elif row.tag == "GrossPositionValue":
            gross = float(row.value)
    if available is None or net_liq is None:
        raise SystemExit("could not read account summary")

    per_trade_cap = available * DEFAULT_CONFIG.max_position_pct
    headroom = portfolio_headroom(net_liq, gross or 0.0, DEFAULT_CONFIG)
    print(f"Available ${available:.2f} | per-trade cap ${per_trade_cap:.2f} | headroom ${headroom:.2f}\n")

    for symbol, qty, stop_dist, note in CANDIDATES:
        qualified = ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
        if not qualified:
            print(f"{symbol}: could not qualify contract, skipping")
            continue
        contract = qualified[0]
        [ticker] = ib.reqTickers(contract)
        ask = ticker.ask if ticker.ask and ticker.ask > 0 else ticker.marketPrice()
        if not ask or ask <= 0:
            print(f"{symbol}: no live quote, skipping")
            continue

        entry = round(ask, 2)
        stop = round(entry - stop_dist, 2)
        target = round(entry + DEFAULT_CONFIG.min_risk_reward * stop_dist, 2)
        notional = qty * entry

        if notional > per_trade_cap + 0.01:
            print(f"{symbol}: ${notional:.2f} exceeds per-trade cap ${per_trade_cap:.2f}, skipping")
            continue
        if notional > headroom:
            print(f"{symbol}: ${notional:.2f} exceeds remaining 80% headroom ${headroom:.2f}, skipping")
            continue

        print(f"{symbol}: BUY {qty} @ {entry} (~${notional:.2f})  stop {stop}  target {target}  [{note}]")
        answer = input(f"  Type YES to place this {symbol} bracket order: ").strip().upper()
        if answer not in ("YES", "Y"):
            print(f"  {symbol} skipped.\n")
            continue

        bracket = ib.bracketOrder("BUY", qty, limitPrice=entry,
                                  takeProfitPrice=target, stopLossPrice=stop)
        bracket.parent.tif = "DAY"
        bracket.takeProfit.tif = "GTC"
        bracket.stopLoss.tif = "GTC"
        try:
            for order in bracket:
                ib.placeOrder(contract, order)
            ib.sleep(2)
            print(f"  {symbol} bracket placed. Parent status: {bracket.parent.orderId}\n")
            headroom -= notional
        except Exception as exc:
            print(f"  {symbol} bracket failed ({exc}); trying plain limit entry...")
            from ib_async import LimitOrder

            trade = ib.placeOrder(contract, LimitOrder("BUY", qty, entry))
            ib.sleep(2)
            print(f"  {symbol} plain limit placed (status {trade.orderStatus.status}). "
                  f"NO automatic stop — set stop {stop} manually in TWS.\n")
            headroom -= notional

    print("Done. Open orders now:")
    for t in ib.openTrades():
        print(f"  {t.contract.symbol}: {t.order.action} {t.order.totalQuantity} "
              f"{t.order.orderType} @ {getattr(t.order, 'lmtPrice', '')} [{t.orderStatus.status}]")
    client.disconnect()


if __name__ == "__main__":
    main()
