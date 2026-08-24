"""One full scan-score-risk-act cycle, shared by both service modes.

PAPER: sizes and places a real bracket order immediately, no per-trade
confirmation -- the same behavior as examples/autotrade_paper.py, just
invoked repeatedly by the supervisor instead of once. Requires the caller
to have already verified this is a paper account (see
service.supervisor._verify_account_matches_mode) -- this function also
re-checks defensively before placing anything, since a mistaken order here
is exactly the failure mode the whole project is built to avoid.

LIVE: sizes and QUEUES the candidate to journal.pending_orders. Never calls
ib.placeOrder. A human runs examples/review_pending_orders.py to act on the
queue.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ib_async import IB, Stock

from ibkr.accounts import (
    order_belongs_to, positions_for, resolve_account, tag_map,
)
from ibkr.data import fetch_universe
from ibkr.scanner import scan_universe
from strategy import journal
from strategy.config import DEFAULT_CONFIG
from strategy.data_quality import drop_incomplete_trailing_bar
from strategy.pullback import compute_pullback_frame, evaluate_pullback
from strategy.risk import RiskRejected, size_position
from strategy.risk_limits import AccountState, check_trade_allowed
from strategy.scoring import score_row
from strategy.signals import compute_indicator_frame, evaluate


@dataclass
class CycleResult:
    ran: bool = True
    reason: str = ""
    candidates_evaluated: int = 0
    new_orders_placed: list = field(default_factory=list)  # PAPER mode
    new_orders_queued: list = field(default_factory=list)  # LIVE mode


def _read_account_summary(ib: IB, account: str = "") -> dict:
    """Summary tags for ONE account.

    accountSummary() spans the login, so an unfiltered {tag: value} keeps
    whichever account's row arrived last -- the pipeline would then size
    trades against a balance belonging to a different account.
    """
    return tag_map(ib.accountSummary(), account)


def run_cycle(ib: IB, mode: str, logger, account: str = "") -> CycleResult:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()

    # Resolve before reading anything. On a login with several accounts an
    # unnamed one is not a default, it is a coin toss about whose balance
    # the risk engine sizes against.
    account, why = resolve_account(ib.managedAccounts(), explicit=account)
    if not account:
        return CycleResult(ran=False, reason=why)

    summary = _read_account_summary(ib, account)
    if "AvailableFunds" not in summary or "NetLiquidation" not in summary:
        return CycleResult(ran=False, reason="account summary unavailable")
    available_funds = float(summary["AvailableFunds"])
    net_liq = float(summary["NetLiquidation"])
    gross_position_value = float(summary.get("GrossPositionValue", 0) or 0)

    if mode == "PAPER" and not account.startswith("D"):
        return CycleResult(
            ran=False,
            reason=f"PAPER mode but account {account} is not a paper id")

    open_position_count = len(positions_for(ib, account))

    journal.log_equity_snapshot(net_liq, now)
    baselines = journal.equity_baselines(now_dt)
    account_state = AccountState(
        net_liquidation=net_liq,
        gross_position_value=gross_position_value,
        open_position_count=open_position_count,
        equity_start_of_day=baselines["day"],
        equity_start_of_week=baselines["week"],
        equity_start_of_month=baselines["month"],
    )
    allowed, halt_reason = check_trade_allowed(account_state, DEFAULT_CONFIG)
    if not allowed:
        logger.warning(f"new entries halted: {halt_reason}")
        journal.log_scan(now, 0, available_funds, net_liq, notes=f"halted: {halt_reason}")
        return CycleResult(ran=False, reason=halt_reason)

    symbols = scan_universe(ib)
    scan_id = journal.log_scan(now, len(symbols), available_funds, net_liq)
    logger.info(f"scan: {len(symbols)} candidates")

    data = fetch_universe(ib, symbols)
    result = CycleResult(ran=True)

    held = {p.contract.symbol for p in positions_for(ib, account)}
    pending_symbols = {t.contract.symbol for t in ib.openTrades()
                       if order_belongs_to(t, account)}

    for symbol, df in data.items():
        result.candidates_evaluated += 1
        df, dropped = drop_incomplete_trailing_bar(df)
        if dropped:
            logger.info(f"{symbol}: dropped incomplete trailing bar")
        if len(df) < DEFAULT_CONFIG.warmup_bars:
            continue

        indf = compute_indicator_frame(df, DEFAULT_CONFIG)
        sig = evaluate(indf, DEFAULT_CONFIG)
        score = score_row(indf.iloc[-1], sig, DEFAULT_CONFIG) if sig.passed else None

        candidate_id = journal.log_candidate(
            scan_id, symbol, now, sig.side, sig.passed,
            score=score.total if score else None,
            entry=sig.entry, stop=sig.stop, checks=sig.checks, reason=sig.reason,
        )

        # Two independent long-signal sources: the 4-indicator gate (with its
        # score threshold) and the trend-pullback system. Gate wins if both
        # fire (it carries the score); pullback fills the low-volume-pullback
        # blind spot the gate structurally can't see. Both are long-only here:
        # gate SELLs are logged above but never traded (account can't short).
        strategy_name = entry_sig = None
        if sig.passed and sig.side == "BUY" and score is not None and score.tradeable:
            strategy_name = "gate"
            entry_sig = (sig.entry, sig.stop, None, DEFAULT_CONFIG.min_risk_reward, score.total)
        else:
            psig = evaluate_pullback(compute_pullback_frame(df), DEFAULT_CONFIG)
            if psig.passed:
                strategy_name = "pullback"
                entry_sig = (psig.entry, psig.stop, psig.target, DEFAULT_CONFIG.pullback_min_rr, None)
                candidate_id = journal.log_candidate(
                    scan_id, symbol, now, "BUY", True,
                    entry=psig.entry, stop=psig.stop, checks=psig.checks,
                    reason="pullback: " + psig.reason,
                )

        if strategy_name is None:
            continue
        if symbol in held or symbol in pending_symbols:
            journal.log_decision(symbol, "already_held_or_pending", now, candidate_id)
            continue

        entry_px, stop_px, entry_target, entry_min_rr, entry_score = entry_sig
        try:
            plan = size_position("BUY", entry_px, stop_px, available_funds, DEFAULT_CONFIG,
                                  net_liquidation=net_liq, fractional=True,
                                  target=entry_target, min_rr=entry_min_rr)
        except RiskRejected as exc:
            journal.log_decision(symbol, "sizing_rejected", now, candidate_id, str(exc))
            continue

        if mode == "PAPER":
            # Re-checked per order, not just once per cycle: the guard has to
            # hold at the moment of submission, not at the moment of scanning.
            if not account.startswith("D"):
                logger.error(f"REFUSING to place {symbol}: account {account} is not a paper id")
                continue
            contract = Stock(symbol, "SMART", "USD")
            qualified = ib.qualifyContracts(contract)
            if not qualified:
                continue
            bracket = ib.bracketOrder(
                plan.side, plan.quantity,
                limitPrice=round(plan.entry, 2),
                takeProfitPrice=round(plan.target, 2),
                stopLossPrice=round(plan.stop, 2),
            )
            for order in bracket:
                order.tif = "GTC"
                order.account = account
                ib.placeOrder(qualified[0], order)
            ib.sleep(1)
            journal.log_decision(symbol, "paper_order_placed", now, candidate_id,
                                  f"strategy={strategy_name} qty={plan.quantity} entry={plan.entry} stop={plan.stop}")
            result.new_orders_placed.append(symbol)
            logger.info(f"PAPER[{strategy_name}]: placed {symbol} {plan.side} {plan.quantity} @ {plan.entry}")
        else:  # LIVE -- queue only, never place
            journal.queue_pending_order(
                symbol, plan.side, plan.quantity, plan.entry, plan.stop, plan.target,
                queued_at=now, score=entry_score,
            )
            journal.log_decision(symbol, "queued_for_review", now, candidate_id,
                                  f"strategy={strategy_name} score={entry_score} qty={plan.quantity}")
            result.new_orders_queued.append(symbol)
            logger.info(f"LIVE[{strategy_name}]: queued {symbol} for review (score {entry_score})")

    return result
