"""Place a core-holding (buy-and-hold index ETF) entry through your own TWS or IB Gateway.

Run this ON YOUR MACHINE while TWS or IB Gateway is open and logged in:

    python examples/place_core_holding.py QQQ

    # IB Gateway listens on 4001 (live) / 4002 (paper), not TWS's 7496:
    IBKR_PORT=4001 python examples/place_core_holding.py QQQ

Why this exists instead of typing the ticket by hand: the TWS Order Entry
panel pre-fills a quantity, does not know about this account's caps, and
has to be re-priced by hand every time the quote drifts. This script reads
your *actual* settled cash at the moment you run it, sizes through
strategy.risk.size_core_holding (70% ETF cap, 80% deployed cap, whitelist
check), and prices the limit off the live ask.

It sizes against the LIMIT price, not the ask — i.e. against the worst fill
the order can get — so a fill at the limit still lands inside the cap.
Under-deploying by a few dollars is free; breaching the cap is not.

Nothing is submitted until you type YES. That keystroke is the owner
confirmation this repo's execution policy requires.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import Stock

from ibkr.client import IBKRClient
from ibkr.orders import place_limit_order_on_contract
from strategy.config import DEFAULT_CONFIG
from strategy.core_holdings import CORE_ETFS
from strategy.risk import RiskRejected, size_core_holding

NY = ZoneInfo("America/New_York")


def _account_value(ib, tag: str) -> float:
    """Pull a single account tag as a float, USD only."""
    for row in ib.accountValues():
        if row.tag == tag and row.currency in ("USD", ""):
            return float(row.value)
    raise RuntimeError(f"account value {tag} not reported by TWS")


def _market_is_open(now_ny: datetime) -> bool:
    if now_ny.weekday() >= 5:
        return False
    return (now_ny.hour, now_ny.minute) >= (9, 30) and (now_ny.hour, now_ny.minute) < (16, 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbol", help=f"whitelisted core ETF, one of: {', '.join(sorted(CORE_ETFS))}")
    parser.add_argument(
        "--buffer-pct",
        type=float,
        default=0.3,
        help="how far above the ask to set the limit, in percent (default 0.3). "
        "A buy limit above the ask still fills AT the ask — the buffer only "
        "stops the order going un-marketable while it sits.",
    )
    args = parser.parse_args()

    symbol = args.symbol.upper()
    if symbol not in CORE_ETFS:
        print(f"REFUSED: {symbol} is not on the core-holding whitelist (strategy/core_holdings.py).")
        print("Adding a symbol there is a deliberate act — leveraged/inverse ETFs are excluded by design.")
        sys.exit(1)

    now_ny = datetime.now(NY)
    print(f"Market clock: {now_ny:%Y-%m-%d %H:%M} ET", end="  ")
    if _market_is_open(now_ny):
        print("(regular session OPEN)")
    else:
        print("(regular session CLOSED)")
        print("A DAY order placed now will not work until the next open, and a limit")
        print("priced off a stale quote is wrong against that open. Re-run at the open.")
        sys.exit(1)

    client = IBKRClient()
    ib = client.connect(retries=5)
    try:
        accounts = ib.managedAccounts()
        print(f"Connected on {client.host}:{client.port}. Accounts: {accounts}")
        if any(a.startswith("D") for a in accounts):
            print("NOTE: PAPER account — this will be a paper trade.")
        else:
            print("*** LIVE ACCOUNT — this spends real money. ***")

        contract = ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
        if not contract:
            raise RuntimeError(f"could not qualify {symbol} on SMART/USD")
        contract = contract[0]

        [ticker] = ib.reqTickers(contract)
        ask = float(ticker.ask)
        if not (ask > 0):
            raise RuntimeError(f"no live ask for {symbol} (got {ticker.ask!r}) — is market data subscribed?")

        limit_price = round(ask * (1 + args.buffer_pct / 100), 2)

        available = _account_value(ib, "AvailableFunds")
        net_liq = _account_value(ib, "NetLiquidation")
        gross = _account_value(ib, "GrossPositionValue")

        plan = size_core_holding(
            symbol=symbol,
            price=limit_price,  # size against the WORST fill, not the ask
            available_funds=available,
            net_liquidation=net_liq,
            gross_position_value=gross,
            config=DEFAULT_CONFIG,
            fractional=True,
        )

        worst = plan.quantity * limit_price
        likely = plan.quantity * ask
        commission = min(max(0.005 * plan.quantity, 1.00), 0.01 * likely)

        print(f"\n  BUY {plan.quantity:.4f} {symbol}  LMT {limit_price:.2f}  DAY")
        print(f"    live ask            ${ask:,.2f}   (limit is {args.buffer_pct:.2f}% above)")
        print(f"    available funds     ${available:,.2f}")
        print(f"    likely fill value   ${likely:,.2f}")
        print(f"    worst case (at lmt) ${worst:,.2f}  vs cap ${plan.position_value:,.2f}  [{plan.binding_cap}]")
        print(f"    est. commission     ${commission:,.2f}  ({commission / likely:.2%} of position)")
        print(f"    % available funds   {plan.pct_of_available_funds:.1%}  (ETF cap {DEFAULT_CONFIG.etf_max_position_pct:.0%})")
        print(f"    % of equity         {plan.pct_of_equity:.1%}  (deployed cap {DEFAULT_CONFIG.max_deployed_pct:.0%})")
        print(f"    cash after          ${available - likely - commission:,.2f}")
        print("\n  No stop, no target — this is a buy-and-hold entry (see CLAUDE.md).")

        answer = input("\nType YES to submit this live order: ").strip()
        if answer != "YES":
            print("Not submitted.")
            return

        trade = place_limit_order_on_contract(
            ib, contract, "BUY", plan.quantity, limit_price, confirm=True
        )
        ib.sleep(2)
        status = trade.orderStatus
        print(f"\nSubmitted. status={status.status} filled={status.filled} avgFill={status.avgFillPrice}")
        if status.status not in ("Filled", "Submitted", "PreSubmitted"):
            print(f"Unexpected status — check the TWS Orders tab. log: {trade.log}")
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
