"""Report current portfolio risk-limit status: daily/weekly/monthly
drawdown vs their limits, position count, and exposure -- the same check
run_scan.py performs before scanning, exposed standalone so it can be
checked any time without kicking off a full scan.

Run ON YOUR MACHINE while TWS is open and logged in:

    python examples/check_risk_limits.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibkr.client import IBKRClient
from strategy import journal
from strategy.config import DEFAULT_CONFIG
from strategy.risk_limits import AccountState, check_trade_allowed


def main() -> None:
    with IBKRClient() as client:
        ib = client.ib

        net_liq = gross_position_value = None
        for row in ib.accountSummary():
            if row.tag == "NetLiquidation":
                net_liq = float(row.value)
            elif row.tag == "GrossPositionValue":
                gross_position_value = float(row.value)
        if net_liq is None:
            raise RuntimeError("could not read NetLiquidation from TWS")
        open_position_count = len([p for p in ib.positions() if p.position != 0])

        now = datetime.now(timezone.utc)
        journal.log_equity_snapshot(net_liq, now.isoformat())
        baselines = journal.equity_baselines(now)

        state = AccountState(
            net_liquidation=net_liq,
            gross_position_value=gross_position_value or 0.0,
            open_position_count=open_position_count,
            equity_start_of_day=baselines["day"],
            equity_start_of_week=baselines["week"],
            equity_start_of_month=baselines["month"],
        )
        allowed, reason = check_trade_allowed(state, DEFAULT_CONFIG)

        print(f"Net liquidation: ${net_liq:.2f}")
        print(f"Gross position value: ${gross_position_value or 0:.2f} "
              f"({(gross_position_value or 0) / net_liq:.0%} of equity, cap {DEFAULT_CONFIG.max_deployed_pct:.0%})")
        print(f"Open positions: {open_position_count}/{DEFAULT_CONFIG.max_concurrent_positions}")

        def pct_line(label, baseline, limit):
            if baseline is None:
                print(f"  {label}: no baseline yet (first snapshot of the period)")
                return
            dd = max(0.0, (baseline - net_liq) / baseline) if baseline > 0 else 0.0
            print(f"  {label}: {dd:+.1%} vs limit -{limit:.0%}  (baseline ${baseline:.2f})")

        print("\nDrawdown:")
        pct_line("Today", baselines["day"], DEFAULT_CONFIG.daily_loss_limit_pct)
        pct_line("This week", baselines["week"], DEFAULT_CONFIG.weekly_drawdown_limit_pct)
        pct_line("This month", baselines["month"], DEFAULT_CONFIG.monthly_drawdown_limit_pct)

        print(f"\nNew entries allowed: {allowed}" + (f"  ({reason})" if reason else ""))


if __name__ == "__main__":
    main()
