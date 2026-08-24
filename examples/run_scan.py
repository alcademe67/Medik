"""Full scan pipeline: IBKR market scanner -> indicator gate -> scoring ->
risk sizing -> journal.

Prints every symbol that passes the 4-indicator gate, its 0-100 score, and
(for score >= 90) the exact entry/stop/target and share count that respects
the 20%-of-available-funds cap and 1:3 R:R. Every candidate and decision is
logged to journal.sqlite regardless of outcome, so there's a durable record
of what the scan saw and why each name was or wasn't traded.

Does NOT place any orders -- review the output and act deliberately.

Usage (TWS must be open and logged in):
    python examples/run_scan.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibkr.accounts import account_context, positions_for
from ibkr.client import IBKRClient
from ibkr.data import fetch_universe
from ibkr.scanner import scan_universe
from strategy import journal
from strategy.config import DEFAULT_CONFIG
from strategy.data_quality import drop_incomplete_trailing_bar
from strategy.risk import RiskRejected, size_position
from strategy.risk_limits import AccountState, check_trade_allowed
from strategy.scoring import score_row
from strategy.signals import compute_indicator_frame, evaluate


def main() -> None:
    with IBKRClient() as client:
        ib = client.ib

        account, summary = account_context(ib)
        if "AvailableFunds" not in summary or "NetLiquidation" not in summary:
            raise RuntimeError(f"could not read account summary for {account}")
        available_funds = float(summary["AvailableFunds"])
        net_liq = float(summary["NetLiquidation"])
        gross_position_value = float(summary.get("GrossPositionValue", 0) or 0)
        open_position_count = len(positions_for(ib, account))
        print(f"Account: {account}  Available funds: {available_funds:.2f}  "
              f"Net liq: {net_liq:.2f}  Open positions: {open_position_count}")

        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        journal.log_equity_snapshot(net_liq, now)
        baselines = journal.equity_baselines(now_dt)
        account_state = AccountState(
            net_liquidation=net_liq,
            gross_position_value=gross_position_value or 0.0,
            open_position_count=open_position_count,
            equity_start_of_day=baselines["day"],
            equity_start_of_week=baselines["week"],
            equity_start_of_month=baselines["month"],
        )
        allowed, reason = check_trade_allowed(account_state, DEFAULT_CONFIG)
        if not allowed:
            print(f"\nNEW ENTRIES HALTED: {reason}")
            journal.log_scan(now, 0, available_funds, net_liq, notes=f"halted: {reason}")
            return

        symbols = scan_universe(ib)
        print(f"Scanner returned {len(symbols)} candidates: {symbols}")

        scan_id = journal.log_scan(now, len(symbols), available_funds, net_liq)

        data = fetch_universe(ib, symbols)
        hits = []
        for symbol, df in data.items():
            df, dropped = drop_incomplete_trailing_bar(df)
            if dropped:
                print(f"  {symbol}: dropped incomplete trailing bar (session not yet closed)")
            if len(df) < DEFAULT_CONFIG.warmup_bars:
                continue
            indf = compute_indicator_frame(df, DEFAULT_CONFIG)
            sig = evaluate(indf, DEFAULT_CONFIG)

            score = score_row(indf.iloc[-1], sig, DEFAULT_CONFIG) if sig.passed else None
            candidate_id = journal.log_candidate(
                scan_id, symbol, now, sig.side, sig.passed,
                score=score.total if score else None,
                entry=sig.entry, stop=sig.stop, checks=sig.checks, reason=sig.reason,
                db_path=journal.DEFAULT_DB_PATH,
            )

            if not sig.passed:
                continue
            if not score.tradeable:
                print(f"  {symbol}: passed gate but score {score.total}/100 < 90 threshold, skipping")
                journal.log_decision(symbol, "score_below_threshold", now, candidate_id, f"score={score.total}")
                continue
            try:
                plan = size_position(sig.side, sig.entry, sig.stop, available_funds, DEFAULT_CONFIG,
                                      net_liquidation=net_liq, fractional=True)
            except RiskRejected as exc:
                print(f"  {symbol}: signal passed (score {score.total}) but sizing rejected: {exc}")
                journal.log_decision(symbol, "sizing_rejected", now, candidate_id, str(exc))
                continue
            hits.append((symbol, sig, score, plan, candidate_id))

        if not hits:
            print("\nNo symbol passed the full gate + score threshold today.")
            return

        hits.sort(key=lambda h: h[2].total, reverse=True)
        print(f"\n{len(hits)} qualifying setup(s), ranked by score:")
        for symbol, sig, score, plan, candidate_id in hits:
            print(
                f"  {symbol}: score {score.total}/100  {plan.side} {plan.quantity} @ ~{plan.entry:.2f}, "
                f"stop {plan.stop:.2f}, target {plan.target:.2f} "
                f"(risk ${plan.capital_at_risk:.2f}, R:R 1:{plan.risk_reward:.0f})"
            )
            journal.log_decision(symbol, "ranked_tradeable", now, candidate_id, f"score={score.total}")


if __name__ == "__main__":
    main()
