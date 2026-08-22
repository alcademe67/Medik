"""Operational safety for the MEDIK ETF live loop: kill switch and recovery.

These are the two things that separate "runs correctly" from "runs correctly
when something goes wrong". Pure functions, so both are testable without a
broker.

KILL SWITCH
    A file at the repo root (STOP_MEDIK) stops the bot. A file is used
    deliberately: it works from another terminal, over RDP, from a scheduled
    task, or from anything that can touch the filesystem -- no signal
    handling, no open terminal, and no dependence on the process still being
    responsive. Creating it is one command; deleting it re-arms.

POSITION RECONCILIATION
    The loop tracks its open position in memory. A restart therefore loses
    the entry, stop and target, and the bot would believe it is flat while
    IBKR says otherwise. It would not double-buy -- the one-position rule
    blocks that -- but it would silently stop MANAGING the position, quietly
    downgrading an actively-managed trade to a bare bracket.

    adopt_open_position() rebuilds the OpenTrade from the broker's own
    working orders, so a restart resumes management. When the protective
    legs cannot be found the position is UNPROTECTED, and that is reported
    as such rather than adopted on optimistic assumptions.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from strategy.medik_etf import OpenTrade, Position

REPO_ROOT = Path(__file__).resolve().parent.parent
KILL_SWITCH_PATH = REPO_ROOT / "STOP_MEDIK"


def kill_switch_active(path: Path = KILL_SWITCH_PATH) -> bool:
    """True when the stop file exists.

    Any error reading the filesystem is treated as NOT active, so a
    transient IO problem cannot silently halt a bot that is holding a
    position. The bot stops on an explicit file, never on an ambiguity.
    """
    try:
        return path.exists()
    except OSError:
        return False


def kill_switch_reason(path: Path = KILL_SWITCH_PATH) -> str:
    """Any text inside the stop file, for the log. Empty is fine."""
    try:
        return path.read_text().strip()[:200]
    except (OSError, UnicodeDecodeError):
        return ""


# --------------------------------------------------------- reconciliation


@dataclass(frozen=True)
class WorkingOrder:
    """One live order at the broker, reduced to what reconciliation needs."""
    symbol: str
    action: str            # "BUY" | "SELL"
    order_type: str        # "STP" | "LMT" | "MKT" | ...
    quantity: float
    price: float           # limit price, or stop trigger


@dataclass(frozen=True)
class Reconciliation:
    adopted: OpenTrade | None
    protected: bool
    reason: str


def adopt_open_position(
    position: Position,
    working_orders: list[WorkingOrder],
    entered_ts: float = 0.0,
) -> Reconciliation:
    """Rebuild an OpenTrade for a position found at startup.

    A long position's protective legs are both SELL orders: a stop (STP)
    below the market and a take-profit (LMT) above it. Both must be present
    for the position to count as protected -- one leg is not a bracket.

    Entry price is unknown after a restart (the fill is in the past), so
    averageCost is used, which is what the broker reports and what any
    R-multiple should be measured from anyway.
    """
    if not position.quantity:
        return Reconciliation(None, False, "no position")

    legs = [o for o in working_orders
            if o.symbol == position.symbol and o.action == "SELL"]
    stops = [o for o in legs if o.order_type in ("STP", "STP LMT")]
    targets = [o for o in legs if o.order_type == "LMT"]

    if not stops and not targets:
        return Reconciliation(
            None, False,
            f"{position.symbol}: position of {position.quantity} held with NO "
            "protective orders — UNPROTECTED")
    if not stops:
        return Reconciliation(
            None, False,
            f"{position.symbol}: take-profit present but NO STOP — UNPROTECTED")
    if not targets:
        return Reconciliation(
            None, False,
            f"{position.symbol}: stop present but no take-profit — protected "
            "against loss, cannot manage the exit")

    entry = (position.market_value / position.quantity) if position.quantity else 0.0
    stop = max(o.price for o in stops)        # tightest stop wins
    target = min(o.price for o in targets)    # nearest target wins

    covered = min(sum(o.quantity for o in stops), sum(o.quantity for o in targets))
    if covered + 1e-9 < abs(position.quantity):
        return Reconciliation(
            None, False,
            f"{position.symbol}: protective legs cover {covered} of "
            f"{position.quantity} shares — PARTIALLY UNPROTECTED")

    trade = OpenTrade(symbol=position.symbol, quantity=int(position.quantity),
                      entry=entry, stop=stop, target=target, entered_ts=entered_ts)
    return Reconciliation(
        trade, True,
        f"{position.symbol}: adopted {position.quantity} shares, entry ~${entry:,.2f}, "
        f"stop ${stop:,.2f}, target ${target:,.2f}")


@dataclass(frozen=True)
class StartupDecision:
    """Outcome of comparing broker state against bot state.

        START  -- flat, nothing to reconcile
        ADOPT  -- a position with a valid bracket; resume managing it
        REFUSE -- do not trade; a human must look
    """
    action: str                    # "START" | "ADOPT" | "REFUSE"
    adopted: OpenTrade | None
    notes: tuple

    @property
    def may_trade(self) -> bool:
        return self.action in ("START", "ADOPT")


def reconcile_startup(
    positions: list[Position],
    working_orders: list[WorkingOrder],
    universe: list[str],
    ignore_symbols: tuple = (),
) -> StartupDecision:
    """Compare broker state against bot state and decide whether to run.

    REFUSING on an unexpected position is deliberate. A position the bot did
    not open means its model of the account is already wrong, and a trading
    loop that is wrong about what it holds is more dangerous than one that
    does not start. A human should look.

    `ignore_symbols` is the deliberate escape hatch: a long-term holding the
    operator has consciously decided this strategy should ignore (the QQQ
    core position, for example) can be listed there. Leaving it empty is the
    safe default -- exemptions should be typed out, not inferred.
    """
    notes: list[str] = []
    adopted: OpenTrade | None = None
    refuse = False

    for pos in positions:
        if not pos.quantity:
            continue

        if pos.symbol in ignore_symbols:
            notes.append(f"{pos.symbol}: {pos.quantity} shares — explicitly ignored "
                         "by configuration, not managed by this strategy")
            continue

        if pos.symbol not in universe:
            notes.append(f"{pos.symbol}: UNEXPECTED position of {pos.quantity} shares, "
                         "outside the ETF universe and not in ignore_symbols")
            refuse = True
            continue

        result = adopt_open_position(pos, working_orders)
        notes.append(result.reason)
        if result.adopted is not None:
            if adopted is None:
                adopted = result.adopted
            else:
                notes.append("more than one adoptable position — the one-position "
                             "rule is already violated")
                refuse = True
        else:
            refuse = True          # position without a valid bracket

    if refuse:
        return StartupDecision("REFUSE", None, tuple(notes))
    if adopted is not None:
        return StartupDecision("ADOPT", adopted, tuple(notes))
    return StartupDecision("START", None, tuple(notes or ["flat at startup"]))
