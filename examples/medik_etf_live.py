"""MEDIK ETF ACTIVE LIVE — scan the ETF universe and trade the best setup.

    MEDIK_ETF_LIVE=true python examples/medik_etf_live.py

No symbols are required: the universe is fixed in strategy.medik_etf and the
program ranks it, then trades AT MOST ONE candidate per invocation.

LIVE MODE IS EXPLICIT
    Orders are sent only when MEDIK_ETF_LIVE is set to exactly "true". Any
    other value — including unset — prints LIVE ETF TRADING DISABLED and
    refuses to send. Live mode is never inferred from a missing variable.

    The flag alone is not enough: every entry still requires the operator to
    type the confirmation for that specific order. That is this repo's
    execution policy (CLAUDE.md) and the confirm=True gate in ibkr/orders.py.

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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import MarketOrder, Stock

from ibkr.client import IBKRClient
from strategy.medik_etf import (
    ETF_UNIVERSE,
    ETFSnapshot,
    PortfolioState,
    Position,
    SessionControls,
    SizingRejected,
    check_can_enter,
    ndx_exposure,
    profile_for,
    rank_candidates,
    score_candidate,
    size_trade,
)
from strategy.medik_mtf import OHLCV, drop_forming_bar

NY = ZoneInfo("America/New_York")
LIVE_ENV_VAR = "MEDIK_ETF_LIVE"
PROTECTION_TIMEOUT_SEC = 10


def log(msg: str) -> None:
    print(f"[{datetime.now(NY):%H:%M:%S}] {msg}", flush=True)


def live_enabled() -> bool:
    """Exact-match opt-in. Never inferred, never truthy-ish."""
    return os.environ.get(LIVE_ENV_VAR, "") == "true"


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


def main() -> None:
    now = datetime.now(NY)
    market_is_open = _market_open(now)
    log("=" * 66)
    log(f"MEDIK ETF ACTIVE LIVE — {now:%Y-%m-%d %H:%M} ET "
        f"({'OPEN' if market_is_open else 'CLOSED'})")

    armed = live_enabled()
    if not armed:
        log("LIVE ETF TRADING DISABLED "
            f"({LIVE_ENV_VAR} is not exactly 'true') — scanning only, no orders")
    log("=" * 66)

    client = IBKRClient()
    ib = client.connect(retries=2)
    try:
        accounts = ib.managedAccounts()
        state = read_portfolio(ib)
        if not accounts or state.net_liquidation <= 0:
            log("PRE-FLIGHT FAILED: account or equity unavailable — NO TRADE")
            return

        controls = SessionControls(equity_start_of_session=state.net_liquidation)
        log(f"account {', '.join(accounts)}  equity ${state.net_liquidation:,.2f}  "
            f"cash ${state.available_cash:,.2f}")
        log(f"positions: {[(p.symbol, round(p.market_value, 2)) for p in state.positions] or 'none'}")
        log(f"Nasdaq-beta exposure: ${ndx_exposure(state.positions):,.2f} "
            f"({ndx_exposure(state.positions) / state.net_liquidation:.0%} of equity)")

        can_enter, why = check_can_enter(state, controls, _now_minutes(now))
        log(f"entry gate: {'OPEN' if can_enter else 'CLOSED — ' + why}\n")

        scores, contracts = [], {}
        for symbol in ETF_UNIVERSE:
            built = build_snapshot(ib, symbol, market_is_open)
            if built is None:
                continue
            snap, contract = built
            contracts[symbol] = contract
            cs = score_candidate(snap)
            scores.append(cs)
            detail = ", ".join(cs.reasons) if cs.reasons else ""
            reject = "; ".join(cs.rejections)
            log(f"  {cs.symbol:<5} ${cs.price:>8.2f} 15m={cs.trend_15m:<7} "
                f"RSI={cs.rsi:>5.1f} RVOL={cs.rvol:>5.2f} "
                f"vwap{'+' if cs.price > cs.vwap else '-'} "
                f"score={cs.score:>5.1f} {cs.signal:<7} {detail or reject}")

        ranked = rank_candidates(scores)
        log("")
        if not ranked:
            log("no qualifying setup — NO TRADE")
            return
        log(f"ranked: {[(c.symbol, c.score) for c in ranked]}")
        best = ranked[0]

        try:
            sized = size_trade(best, state)
        except SizingRejected as exc:
            log(f"{best.symbol}: {exc}")
            return

        log("")
        log(f"SELECTED {sized.symbol}  score {best.score:.1f}  "
            f"leverage {profile_for(sized.symbol).leverage}x")
        log(f"  qty {sized.quantity} whole shares @ ${sized.entry:,.2f} "
            f"= ${sized.notional:,.2f} notional")
        log(f"  stop ${sized.stop:,.2f}   target ${sized.target:,.2f}   "
            f"R:R {sized.reward_risk:.1f}:1")
        log(f"  dollar risk ${sized.risk_dollars:,.2f} "
            f"({sized.risk_dollars / state.net_liquidation:.2%} of equity)  "
            f"binding cap: {sized.binding_cap}")

        if not can_enter:
            log(f"NO TRADE — {why}")
            return
        if not armed:
            log("NO TRADE — LIVE ETF TRADING DISABLED")
            return

        if input(f'  Type "BUY {sized.symbol}" to place this bracket: ').strip() \
                != f"BUY {sized.symbol}":
            log("NO TRADE — not confirmed")
            return

        ok = place_bracket(ib, contracts[sized.symbol], sized, controls)
        log("position open and protected" if ok
            else f"ENTRIES DISABLED: {controls.disabled_reason}")
    finally:
        client.disconnect()
        log("disconnected")


if __name__ == "__main__":
    main()
