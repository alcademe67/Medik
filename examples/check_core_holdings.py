"""Status report for long-term core ETF holdings.

This is a MONITOR, not a trading loop. Buy-and-hold's edge comes from not
acting; the backtest that motivated the strategy showed every active overlay
tested returned less than simply holding. So this script deliberately emits
no buy/sell recommendation and has no sell trigger.

In particular, a drawdown line here is NOT a signal to sell. QQQ fell 22.9%
inside the 5-year window that returned +121.8%, and 35.6% in 2022. Selling
into either of those is how a holder converts a winning strategy into a
losing one. The number is shown for awareness only.

Also reports how much additional room the current rules allow, which is
useful when adding to the position with new contributions.

Run ON YOUR MACHINE while TWS is open and logged in:

    python examples/check_core_holdings.py                 # QQQ, headroom in dollars
    python examples/check_core_holdings.py QQQ 722.74      # ... and in shares
    python examples/check_core_holdings.py --save          # also write to reports/
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibkr.client import IBKRClient
from paths import report_path
from strategy.config import DEFAULT_CONFIG
from strategy.core_holdings import is_core_etf
from strategy.risk import RiskRejected, portfolio_headroom, size_core_holding
from strategy.core_holdings import NotACoreETF

_ARGS = [a for a in sys.argv[1:] if not a.startswith("-")]
SAVE = "--save" in {a for a in sys.argv[1:] if a.startswith("-")}


def main() -> None:
    config = DEFAULT_CONFIG

    with IBKRClient() as client:
        ib = client.ib

        net_liq = available_funds = gross_position_value = None
        for row in ib.accountSummary():
            if row.tag == "NetLiquidation":
                net_liq = float(row.value)
            elif row.tag == "AvailableFunds":
                available_funds = float(row.value)
            elif row.tag == "GrossPositionValue":
                gross_position_value = float(row.value)

        if net_liq is None:
            raise RuntimeError("could not read NetLiquidation from TWS")
        available_funds = available_funds or 0.0
        gross_position_value = gross_position_value or 0.0

        positions = [p for p in ib.positions() if p.position != 0]

    core, other = [], []
    for p in positions:
        (core if is_core_etf(p.contract.symbol) else other).append(p)

    print(f"\nEquity ${net_liq:,.2f}   cash available ${available_funds:,.2f}   "
          f"invested ${gross_position_value:,.2f} "
          f"({gross_position_value / net_liq:.1%} of equity)")

    print("\n--- core ETF holdings (long-term, no stop) ---")
    if not core:
        print("  none")
    core_value = 0.0
    for p in core:
        cost = p.avgCost * abs(p.position)
        mkt = getattr(p, "marketValue", None)
        line = f"  {p.contract.symbol:<6} {p.position:>12,.6f} sh   cost ${cost:>10,.2f}"
        if mkt:
            pnl = mkt - cost
            line += (f"   value ${mkt:>10,.2f}   "
                     f"P&L ${pnl:>+9,.2f} ({pnl / cost:+.1%})" if cost else "")
            core_value += mkt
        print(line)
    if core_value:
        print(f"  {'':<6} {'':>12}      core = {core_value / net_liq:.1%} of equity")

    if other:
        print("\n--- other positions (not core ETFs) ---")
        for p in other:
            print(f"  {p.contract.symbol:<6} {p.position:>12,.6f} sh   "
                  f"cost ${p.avgCost * abs(p.position):>10,.2f}")
        print("  These consume headroom under the "
              f"{config.max_deployed_pct:.0%} deployed cap.")

    headroom = portfolio_headroom(net_liq, gross_position_value, config)
    print(f"\nHeadroom under the {config.max_deployed_pct:.0%} deployed cap: ${headroom:,.2f}")

    symbol = _ARGS[0].upper() if _ARGS else "QQQ"
    print(f"\nRoom to add to {symbol} under current rules:")
    try:
        # price is only needed to convert dollars->shares; ask the user to
        # supply it rather than pulling a quote, so this stays read-only.
        price = float(_ARGS[1]) if len(_ARGS) > 1 else None
        if price is None:
            allowed = min(available_funds * config.etf_max_position_pct, headroom)
            print(f"  ${allowed:,.2f}  (pass a price as arg 2 for a share count)")
        else:
            plan = size_core_holding(symbol, price, available_funds, net_liq,
                                     gross_position_value, config)
            print(f"  {plan.quantity:,.6f} sh  = ${plan.position_value:,.2f} "
                  f"({plan.pct_of_available_funds:.1%} of cash, "
                  f"{plan.pct_of_equity:.1%} of equity)")
            print(f"  binding cap: {plan.binding_cap}")
    except (RiskRejected, NotACoreETF) as exc:
        print(f"  none — {exc}")

    print("\nNo action is implied by anything above. This report never "
          "recommends selling a core holding.\n")


class _Tee(io.StringIO):
    """Captures output for the report file while still printing it live."""

    def write(self, s: str) -> int:
        sys.__stdout__.write(s)
        return super().write(s)


if __name__ == "__main__":
    if SAVE:
        buffer = _Tee()
        with redirect_stdout(buffer):
            main()
        # reports/ is gitignored -- this output carries live balances and P&L.
        destination = report_path("core-holdings", "txt")
        destination.write_text(buffer.getvalue(), encoding="utf-8")
        print(f"\nsaved -> {destination}")
    else:
        main()
