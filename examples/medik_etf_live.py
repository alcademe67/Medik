"""MEDIK ETF ACTIVE LIVE — scan the ETF universe and trade the best setup.

    MEDIK_ETF_LIVE=true python examples/medik_etf_live.py

No symbols are required: the universe is fixed in strategy.medik_etf. The
process RUNS CONTINUOUSLY through the regular session, scanning every
SCAN_INTERVAL_SEC, holding at most one position at a time.

LIVE MODE IS EXPLICIT, AND AUTHORISES AUTOMATIC SUBMISSION
    Orders are sent only when MEDIK_ETF_LIVE is set to exactly "true". Any
    other value — including unset — prints LIVE ETF TRADING DISABLED and
    refuses to send. Live mode is never inferred from a missing variable.

    Once enabled, qualifying orders are submitted AUTOMATICALLY with no
    per-order human approval. This is an owner-authorised exception to the
    repo's general draft-and-approve policy, scoped to this strategy alone
    and recorded in CLAUDE.md; every other order path still requires a human.

    The approval step is REPLACED, not removed. strategy.medik_etf's
    authorize_order() runs a deterministic checklist — live flag, connection,
    account data, buying power, market data, setup validity, whole-share
    size, stop, target, reward/risk, risk ceiling, capital utilisation,
    conflicting position, conflicting order, duplicate suppression, session
    gates — and an order is sent only when EVERY check passes. The function
    is pure, so identical inputs always give an identical decision. No model
    output authorises a trade.

EVERY TRADE IS A BUY
    The account cannot short. Bearish views are expressed by buying the
    inverse fund (SQQQ, SOXS, TZA, LABD, FAZ, ERY), each scored on its own
    price action rather than as a mechanical inversion of its bull twin.

BRACKET PROTECTION IS MANDATORY
    A position is not considered protected until the parent, stop and target
    are all acknowledged by IBKR. If the parent fills and either protective
    leg fails, the program attempts to re-establish protection; failing that
    it flattens the position with a marketable order, logs the exact IBKR
    error, and disables further entries for the run.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import MarketOrder, Stock

from ibkr.client import IBKRClient
from strategy.medik_etf import (
    ETF_UNIVERSE,
    MAX_CAPITAL_UTILIZATION,
    SCAN_INTERVAL_SEC,
    ETFSnapshot,
    OpenTrade,
    PortfolioState,
    Position,
    SessionControls,
    SizingRejected,
    TradeLedger,
    authorize_order,
    check_can_enter,
    ndx_exposure,
    profile_for,
    rank_candidates,
    score_candidate,
    should_exit,
    should_rotate,
    size_trade,
)
from strategy.medik_etf_ops import (
    WorkingOrder,
    verify_account_mode,
    kill_switch_active,
    kill_switch_reason,
    reconcile_startup,
)
from strategy.medik_mtf import OHLCV, drop_forming_bar

NY = ZoneInfo("America/New_York")
LIVE_ENV_VAR = "MEDIK_ETF_LIVE"
RISK_ACK_ENV_VAR = "LIVE_RISK_ACK"
MODE_ENV_VAR = "MEDIK_ETF_MODE"
PROTECTION_TIMEOUT_SEC = 10

# Holdings this strategy must LEAVE ALONE rather than adopt or refuse over.
# Empty by default on purpose: an exemption should be typed out deliberately.
# The QQQ buy-and-hold core position is the obvious candidate — but note that
# adding it does NOT make the bot able to trade QQQ-correlated ETFs, because
# the Nasdaq-beta cap still counts that exposure.
IGNORE_SYMBOLS: tuple = ()


def log(msg: str) -> None:
    print(f"[{datetime.now(NY):%H:%M:%S}] {msg}", flush=True)


def live_enabled() -> bool:
    """Exact-match opt-in. Never inferred, never truthy-ish."""
    return os.environ.get(LIVE_ENV_VAR, "") == "true"


def risk_acknowledged() -> bool:
    """Second key. Both must be turned for the bot to send an order.

    MEDIK_ETF_LIVE says "this program may trade". LIVE_RISK_ACK says "I have
    read what it is about to risk". They are separate because the first is
    easy to leave set in a shell profile or a scheduled task, and a single
    forgotten variable should not be the only thing between a scan and a
    live order.
    """
    return os.environ.get(RISK_ACK_ENV_VAR, "") == "true"


def arming_report() -> tuple[bool, list[str]]:
    """(armed, human-readable lines) describing exactly which keys are set.

    Also catches plausible-but-wrong variable names. Setting LIVE_TRADING
    instead of MEDIK_ETF_LIVE fails safe, but silently -- the operator would
    see a clean startup and assume it was armed.
    """
    live, ack = live_enabled(), risk_acknowledged()
    lines = [
        f"  {LIVE_ENV_VAR:<16} {'SET' if live else 'not set'}",
        f"  {RISK_ACK_ENV_VAR:<16} {'SET' if ack else 'not set'}",
    ]
    for wrong in ("LIVE_TRADING", "MEDIK_LIVE", "ETF_LIVE"):
        if os.environ.get(wrong):
            lines.append(f"  WARNING: {wrong} is set but is NOT a recognised "
                         f"variable — did you mean {LIVE_ENV_VAR}?")
    return (live and ack), lines


def _to_bars(raw) -> list[OHLCV]:
    return [OHLCV(b.open, b.high, b.low, b.close, float(b.volume or 0)) for b in raw]


def _now_minutes(now: datetime) -> int:
    return now.hour * 60 + now.minute


def _market_open(now: datetime) -> bool:
    return now.weekday() < 5 and 9 * 60 + 30 <= _now_minutes(now) < 16 * 60


# ------------------------------------------------------------ data + scoring


def build_snapshot(ib, symbol: str, market_is_open: bool) -> ETFSnapshot | None:
    qualified = ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
    if not qualified:
        log(f"  {symbol:<5} could not qualify contract")
        return None
    contract = qualified[0]

    bars5 = _to_bars(ib.reqHistoricalData(
        contract, endDateTime="", durationStr="2 D", barSizeSetting="5 mins",
        whatToShow="TRADES", useRTH=True, formatDate=1))
    bars15 = _to_bars(ib.reqHistoricalData(
        contract, endDateTime="", durationStr="5 D", barSizeSetting="15 mins",
        whatToShow="TRADES", useRTH=True, formatDate=1))

    # Closed candles only — a forming bar makes scores flicker as it ticks.
    bars5 = drop_forming_bar(bars5, bar_complete=not market_is_open)
    bars15 = drop_forming_bar(bars15, bar_complete=not market_is_open)
    if len(bars5) < 25 or len(bars15) < 25:
        log(f"  {symbol:<5} insufficient bars (5m={len(bars5)} 15m={len(bars15)})")
        return None

    [ticker] = ib.reqTickers(contract)
    bid = float(ticker.bid or 0)
    ask = float(ticker.ask or 0)
    last = float(ticker.last or ticker.close or bars5[-1].close)
    if not (bid > 0 and ask > 0 and last > 0):
        log(f"  {symbol:<5} no usable quote (bid={bid} ask={ask} last={last})")
        return None

    # VWAP resets daily: pass only the CURRENT session's bars.
    session_bars = bars5[-78:] if len(bars5) > 78 else bars5
    dollar_volume = sum(b.close * b.volume for b in session_bars)

    return ETFSnapshot(
        symbol=symbol, price=last, bid=bid, ask=ask,
        bars_5m=session_bars, bars_15m=bars15,
        session_dollar_volume=dollar_volume,
    ), contract


def read_portfolio(ib) -> PortfolioState:
    values = {r.tag: r.value for r in ib.accountValues() if r.currency in ("USD", "")}
    positions = tuple(
        Position(p.contract.symbol, p.position, p.position * p.averageCost)
        for p in ib.portfolio() if p.position
    ) or tuple(
        Position(p.contract.symbol, p.position, p.position * p.avgCost)
        for p in ib.positions() if p.position
    )
    return PortfolioState(
        net_liquidation=float(values.get("NetLiquidation", 0) or 0),
        available_cash=float(values.get("AvailableFunds", 0) or 0),
        positions=positions,
        open_order_count=len([t for t in ib.openTrades()
                              if t.orderStatus.status not in ("Filled", "Cancelled")]),
    )


# ------------------------------------------------------------------ execution


def verify_protection(ib, trade_legs) -> tuple[bool, str]:
    """A position is protected only when ALL three legs are acknowledged."""
    ib.sleep(PROTECTION_TIMEOUT_SEC)
    dead = ("Cancelled", "ApiCancelled", "Inactive")
    for leg, name in zip(trade_legs, ("parent", "stop", "target")):
        status = leg.orderStatus.status
        if status in dead:
            return False, f"{name} leg is {status}"
        if not status:
            return False, f"{name} leg has no status from IBKR"
    return True, ""


def emergency_shutdown(ib, armed: bool, open_trade, contracts, reason: str) -> bool:
    """STOP_MEDIK sequence, in this exact order:

        disable entries -> cancel working orders -> flatten -> confirm flat

    Cancelling BEFORE flattening is the part that matters. A resting stop is
    a live SELL; if the flatten order goes in while the stop is still
    working, both can fill and the account ends up SHORT -- which this
    account cannot even hold. Cancel first, then close, then verify.
    """
    log("=" * 66)
    log(f"STOP_MEDIK DETECTED — {reason}" if reason else "STOP_MEDIK DETECTED")
    log("=" * 66)
    log("1. new entries disabled")

    log("2. cancelling working orders")
    cancelled = 0
    try:
        ib.reqAllOpenOrders()
        ib.sleep(1)
        for trade in ib.openTrades():
            if trade.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled"):
                continue
            if armed:
                ib.cancelOrder(trade.order)
            cancelled += 1
            log(f"   cancel {trade.order.action} {trade.order.totalQuantity} "
                f"{trade.contract.symbol} {trade.order.orderType} "
                f"(id {trade.order.orderId})")
        if cancelled and armed:
            ib.sleep(3)
    except Exception as exc:
        log(f"   ERROR cancelling orders: {exc!r} — NOT flattening while orders "
            "may still be live; close manually in TWS")
        return False
    log(f"   {cancelled} order(s) handled")

    log("3. flattening open position")
    if open_trade is None:
        log("   no tracked position")
    elif not armed:
        log("   not armed — would flatten, sending nothing")
    else:
        flatten(ib, contracts.get(open_trade.symbol), open_trade.quantity, "STOP_MEDIK")

    log("4. confirming flat")
    try:
        ib.sleep(2)
        remaining = [(p.contract.symbol, p.position) for p in ib.positions() if p.position]
        if not remaining:
            log("   IBKR reports FLAT")
            flat = True
        else:
            log(f"   STILL HOLDING: {remaining}")
            log("   CHECK TWS MANUALLY — the process is exiting but the account is not flat")
            flat = False
    except Exception as exc:
        log(f"   could not verify positions: {exc!r} — CHECK TWS MANUALLY")
        flat = False

    log("5. exiting")
    return flat


def flatten(ib, contract, quantity: int, reason: str) -> None:
    """Emergency exit: close an unprotected position with a marketable order."""
    log(f"  EMERGENCY FLATTEN {contract.symbol} x{quantity} — {reason}")
    order = MarketOrder("SELL", quantity)
    order.tif = "DAY"
    ib.placeOrder(contract, order)
    ib.sleep(5)
    log(f"  flatten status: {order.orderId} -> "
        f"{ib.trades()[-1].orderStatus.status if ib.trades() else 'unknown'}")


def place_bracket(ib, contract, sized, controls: SessionControls) -> bool:
    """Submit entry+stop+target, verify all three, or flatten. Returns success."""
    bracket = ib.bracketOrder(
        "BUY", sized.quantity,
        limitPrice=round(sized.entry, 2),
        takeProfitPrice=round(sized.target, 2),
        stopLossPrice=round(sized.stop, 2),
    )
    # Entry dies at the close (a stale limit is wrong against the next open);
    # protective legs are GTC so they survive as long as the position does.
    bracket[0].tif = "DAY"
    for leg in bracket[1:]:
        leg.tif = "GTC"

    legs = [ib.placeOrder(contract, o) for o in bracket]
    log(f"  submitted: parent={bracket[0].orderId} "
        f"stop={bracket[2].orderId} target={bracket[1].orderId}")

    protected, why = verify_protection(ib, legs)
    parent_filled = legs[0].orderStatus.filled or 0

    if protected:
        for leg, name in zip(legs, ("parent", "stop", "target")):
            log(f"    {name:<7} {leg.orderStatus.status:<12} "
                f"filled={leg.orderStatus.filled}")
        return True

    log(f"  BRACKET FAILURE: {why}")
    if parent_filled > 0:
        flatten(ib, contract, int(parent_filled), why)
    else:
        for o in bracket:
            ib.cancelOrder(o)
        log("  parent unfilled — all legs cancelled")
    controls.disable(f"bracket failure: {why}")
    return False


# ----------------------------------------------------------------------- main


def scan_once(ib, armed: bool, controls: SessionControls, ledger: TradeLedger,
              open_trade: OpenTrade | None,
              contract_sink: dict | None = None) -> OpenTrade | None:
    """One scan cycle. Returns the open trade after this cycle, or None.

    Order of business: read account -> manage any open position -> if flat,
    score the universe, rank it, size the best, run the authorisation
    checklist, and submit automatically if every check passes.
    """
    now = datetime.now(NY)
    market_is_open = _market_open(now)
    now_min = _now_minutes(now)
    now_ts = time.time()

    state = read_portfolio(ib)
    if state.net_liquidation <= 0:
        log("account data unavailable this cycle — NO ORDER")
        return open_trade

    exposure = ndx_exposure(state.positions)
    log(f"equity ${state.net_liquidation:,.2f}  buying power ${state.available_cash:,.2f}  "
        f"NDX-beta exposure ${exposure:,.2f} ({exposure / state.net_liquidation:.0%})")
    log(f"positions: {[(p.symbol, round(p.market_value, 2)) for p in state.positions] or 'none'}")

    # ---- score the universe first: needed to manage the position as well as
    # to find a new one.
    scores, contracts = {}, {}
    for symbol in ETF_UNIVERSE:
        built = build_snapshot(ib, symbol, market_is_open)
        if built is None:
            continue
        snap, contract = built
        contracts[symbol] = contract
        cs = score_candidate(snap)
        scores[symbol] = cs

    if contract_sink is not None:
        contract_sink.update(contracts)   # so a kill switch can flatten

    ranked = rank_candidates(list(scores.values()))
    top = ranked[:5]
    if top:
        log("top candidates: " + ", ".join(
            f"{c.symbol}={c.score:.0f}" for c in top))
    for c in sorted(scores.values(), key=lambda x: -x.score)[:5]:
        detail = ", ".join(c.reasons) if c.signal == "TRADE" else "; ".join(c.rejections)
        log(f"  {c.symbol:<5} ${c.price:>8.2f} 15m={c.trend_15m:<7} "
            f"RSI={c.rsi:>5.1f} RVOL={c.rvol:>5.2f} score={c.score:>5.1f} "
            f"{c.signal:<7} {detail}")

    # ---- manage an existing position
    if open_trade is not None:
        current = scores.get(open_trade.symbol)
        price = current.price if current else open_trade.entry
        exiting, why = should_exit(open_trade, current, price, now_min)
        if exiting:
            log(f"EXIT {open_trade.symbol}: {why}")
            if armed:
                flatten(ib, contracts.get(open_trade.symbol), open_trade.quantity, why)
                controls.trades_completed += 1
            return None
        if ranked:
            rotate, rwhy = should_rotate(open_trade, current, ranked[0], price)
            log(f"rotation: {'YES — ' + rwhy if rotate else 'no — ' + rwhy}")
            if rotate and armed:
                flatten(ib, contracts.get(open_trade.symbol), open_trade.quantity, rwhy)
                controls.trades_completed += 1
                return None
        log(f"holding {open_trade.symbol} x{open_trade.quantity} "
            f"(stop ${open_trade.stop:.2f} target ${open_trade.target:.2f})")
        return open_trade

    # ---- look for a new entry
    if not ranked:
        log("no qualifying setup — NO ORDER")
        return None

    best = ranked[0]
    try:
        sized = size_trade(best, state)
    except SizingRejected as exc:
        log(f"{best.symbol}: {exc} — NO ORDER")
        return None

    log(f"SELECTED {sized.symbol} score {best.score:.1f} "
        f"leverage {profile_for(sized.symbol).leverage}x — {', '.join(best.reasons)}")
    log(f"  allocation ${sized.notional:,.2f} "
        f"({sized.notional / state.available_cash:.0%} of buying power, "
        f"limit {MAX_CAPITAL_UTILIZATION:.0%})  qty {sized.quantity} whole shares")
    log(f"  entry ${sized.entry:,.2f}  stop ${sized.stop:,.2f}  "
        f"target ${sized.target:,.2f}  R:R {sized.reward_risk:.1f}:1")
    log(f"  risk ${sized.risk_dollars:,.2f} "
        f"({sized.risk_dollars / state.net_liquidation:.2%} of equity)  "
        f"binding cap: {sized.binding_cap}")

    auth = authorize_order(
        live_enabled=armed, connected=ib.isConnected(), state=state,
        controls=controls, candidate=best, sized=sized,
        now_minutes=now_min, ledger=ledger, now_ts=now_ts,
    )
    if not auth:
        log(f"NO ORDER — failed checks: {', '.join(auth.failures)}")
        return None

    # Authorised by the checklist alone. No interactive confirmation.
    ledger.mark_pending(sized.symbol)
    ok = place_bracket(ib, contracts[sized.symbol], sized, controls)
    if not ok:
        ledger.mark_failed(sized.symbol)
        log(f"ENTRIES DISABLED: {controls.disabled_reason}")
        return None

    ledger.mark_entered(sized.symbol, now_ts)
    controls.trades_completed += 1
    return OpenTrade(sized.symbol, sized.quantity, sized.entry, sized.stop,
                     sized.target, now_ts)


def main() -> None:
    armed, arming_lines = arming_report()
    log("=" * 66)
    log("MEDIK ETF ACTIVE LIVE")
    for line in arming_lines:
        log(line)
    log(f"AUTOMATIC TRADING: {'ENABLED' if armed else 'DISABLED'}")
    log(f"CAPITAL UTILIZATION: UP TO {MAX_CAPITAL_UTILIZATION:.0%}")
    log(f"SCAN INTERVAL: {SCAN_INTERVAL_SEC // 60} MINUTES")
    if not armed:
        log("LIVE ETF TRADING DISABLED — both "
            f"{LIVE_ENV_VAR}=true and {RISK_ACK_ENV_VAR}=true are required. "
            "Scanning only, no orders will be sent.")

    client = IBKRClient()
    ib = client.connect(retries=2)
    log(f"IBKR: {'CONNECTED' if ib.isConnected() else 'NOT CONNECTED'}")
    log("=" * 66)
    controls = None
    ledger = TradeLedger()
    open_trade: OpenTrade | None = None
    last_contracts: dict = {}

    try:
        accounts = ib.managedAccounts()
        state = read_portfolio(ib)
        if not accounts or state.net_liquidation <= 0:
            log("PRE-FLIGHT FAILED: account or equity unavailable — exiting")
            return

        # Paper must be PROVEN, not assumed from a port number.
        mode = os.environ.get(MODE_ENV_VAR, "")
        mode_ok, mode_msg = verify_account_mode(mode, list(accounts), client.port)
        log(f"mode: {mode_msg}")
        if not mode_ok:
            log(f"PRE-FLIGHT FAILED: set {MODE_ENV_VAR}=paper or {MODE_ENV_VAR}=live "
                "to match the account you are connected to — exiting")
            return
        if mode.strip().lower() == "paper":
            log("PAPER MODE — orders are simulated; the live gate still applies")
        controls = SessionControls(equity_start_of_session=state.net_liquidation)
        log(f"account {', '.join(accounts)}  session equity baseline "
            f"${state.net_liquidation:,.2f}")

        # RECONCILE. A restart loses the in-memory position, so rebuild it
        # from the broker's own working orders. Without this the bot would
        # believe it is flat and silently stop managing a live trade.
        ib.reqAllOpenOrders()
        ib.sleep(1)
        working = [
            WorkingOrder(t.contract.symbol, t.order.action, t.order.orderType,
                         float(t.order.totalQuantity or 0),
                         float(t.order.lmtPrice or t.order.auxPrice or 0))
            for t in ib.openTrades()
            if t.orderStatus.status not in ("Filled", "Cancelled", "ApiCancelled")
        ]
        decision = reconcile_startup(list(state.positions), working, ETF_UNIVERSE,
                                     ignore_symbols=IGNORE_SYMBOLS)
        for note in decision.notes:
            log(f"reconcile: {note}")
        log(f"reconcile: decision = {decision.action}")
        if not decision.may_trade:
            log("REFUSING TO TRADE. The bot's model of the account does not match "
                "the broker. Close the position, restore its bracket in TWS, or add "
                "the symbol to IGNORE_SYMBOLS if it is a holding this strategy "
                "should leave alone.")
            return
        open_trade = decision.adopted
        if open_trade is not None:
            log(f"resuming management of {open_trade.symbol} x{open_trade.quantity}")

        while True:
            now = datetime.now(NY)

            # Checked FIRST, before market hours and before any scan, so the
            # switch works whether or not the session is open.
            if kill_switch_active():
                controls.disable("STOP_MEDIK")
                emergency_shutdown(ib, armed, open_trade, last_contracts,
                                   kill_switch_reason())
                break

            if not _market_open(now):
                if open_trade is not None:
                    log("market closed with a position still open — "
                        "protective legs are GTC and remain working")
                log(f"market closed at {now:%H:%M} ET — exiting loop")
                break

            log("-" * 66)
            log(f"scan @ {now:%H:%M:%S} ET")
            try:
                open_trade = scan_once(ib, armed, controls, ledger, open_trade,
                                       last_contracts)
            except Exception as exc:  # one bad cycle must not kill the run
                controls.execution_errors += 1
                log(f"CYCLE ERROR ({controls.execution_errors}): {exc!r}")
                if controls.execution_errors >= 3:
                    controls.disable(f"repeated execution errors: {exc!r}")
                    log(f"ENTRIES DISABLED: {controls.disabled_reason}")

            if controls.entries_disabled and open_trade is None:
                log(f"entries disabled ({controls.disabled_reason}) and flat — exiting")
                break

            # Sleep in slices so the kill switch is honoured within ~10s
            # rather than after a full scan interval.
            for _ in range(max(1, SCAN_INTERVAL_SEC // 10)):
                if kill_switch_active():
                    break
                ib.sleep(10)
    finally:
        client.disconnect()
        log("disconnected")


if __name__ == "__main__":
    main()
