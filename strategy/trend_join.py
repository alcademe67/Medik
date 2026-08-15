"""Trend Join Long — the strategy described by rules.json.

Long-only intraday momentum on 5-minute bars: take S&P 500 names that gapped
up out of a daily uptrend, and enter when they break to a new high of day on
heavy volume. Stop under the low of day, scale out at 0.75R, move to
breakeven at 1R, then trail behind confirmed 5-minute swing lows.

Everything here is a PURE FUNCTION of bars and numbers. Nothing in this
module imports ib_async, places an order, or reads the clock -- the caller
passes the time in. That is deliberate: it means the whole strategy can be
tested offline (see tests/test_trend_join.py), which matters because the
two strategies already in this repo were only discovered to be unprofitable
once they could be backtested properly.

READ BEFORE TRADING THIS WITH REAL MONEY
----------------------------------------
This is a same-day-in, same-day-out strategy: the time filter forces every
position closed at 15:51 ET. That means paying the full round-trip
commission on every single trade. On this account's schedule
(clamp($0.005/share, min $1.00, max 1% of trade value)) a small position
pays ~2% per round trip, and CLAUDE.md records two strategies that died
exactly there -- 149 pullback trades made $4.72 gross and paid $112.47 in
commissions. Run this on PAPER, log the fills, and put the results through
net_of_commission.py before anyone discusses the live account.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES_PATH = REPO_ROOT / "rules.json"


class RulesError(ValueError):
    """rules.json is missing a field, has an unknown field, or holds a value
    the strategy cannot act on."""


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TrendJoinRules:
    strategy_name: str
    direction: str
    trade_timeframe: str

    # universe
    index: str
    min_price_usd: float

    # daily filters
    d1_above_prior_day_high: bool
    d2_prior_close_above_sma200: bool
    d3_min_gap_pct: float

    # intraday filters
    i1_above_premarket_high: bool
    i2_above_today_hod: bool
    i3_rvol_min: float
    i3_rvol_lookback_days: int

    # time filter (America/New_York wall clock)
    earliest_entry_et: time
    latest_entry_et: time
    force_close_et: time

    # exit
    initial_stop_rule: str
    partial_profit_trigger_r: float
    partial_profit_fraction: float
    breakeven_trigger_r: float
    post_breakeven_trail: str

    # risk
    max_risk_per_trade_pct: float
    max_position_size_pct_of_portfolio: float
    max_concurrent_positions: int


_EXPECTED = {
    "strategy_name", "direction", "trade_timeframe",
    "universe_filters", "daily_filters", "intraday_filters",
    "time_filter", "exit", "risk",
}
_SUPPORTED_STOP_RULES = {"lod_minus_1pct"}
_SUPPORTED_TRAILS = {"swing_low_5m_2_2"}


def _require(section: dict, key: str, name: str):
    if key not in section:
        raise RulesError(f"rules.json: {name} is missing required key {key!r}")
    return section[key]


def _parse_et(value: str, name: str) -> time:
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError) as exc:
        raise RulesError(f"rules.json: {name} must be 'HH:MM', got {value!r}") from exc


def load_rules(path: Path | str = DEFAULT_RULES_PATH) -> TrendJoinRules:
    """Parse and validate rules.json.

    Unknown top-level sections are an ERROR rather than a warning. A typo in
    a config key otherwise fails silently as "filter disabled", which is the
    worst possible failure mode for a trading rule -- the same reasoning
    behind SCAN_ENABLED being opt-in in service/config.py.
    """
    path = Path(path)
    if not path.exists():
        raise RulesError(f"rules.json not found at {path}")
    raw = json.loads(path.read_text())

    unknown = set(raw) - _EXPECTED
    if unknown:
        raise RulesError(f"rules.json: unknown top-level key(s) {sorted(unknown)}")
    missing = _EXPECTED - set(raw)
    if missing:
        raise RulesError(f"rules.json: missing section(s) {sorted(missing)}")

    universe = _require(raw, "universe_filters", "root")
    daily = _require(raw, "daily_filters", "root")
    intraday = _require(raw, "intraday_filters", "root")
    timing = _require(raw, "time_filter", "root")
    exit_ = _require(raw, "exit", "root")
    risk = _require(raw, "risk", "root")

    direction = raw["direction"]
    if direction != "long_only":
        raise RulesError(
            f"rules.json: direction is {direction!r}; this account is long-only "
            "(TFSA, cannot short) and this module implements long entries only"
        )

    stop_rule = _require(exit_, "initial_stop_rule", "exit")
    if stop_rule not in _SUPPORTED_STOP_RULES:
        raise RulesError(
            f"rules.json: initial_stop_rule {stop_rule!r} not implemented "
            f"(supported: {sorted(_SUPPORTED_STOP_RULES)})"
        )
    trail = _require(exit_, "post_breakeven_trail", "exit")
    if trail not in _SUPPORTED_TRAILS:
        raise RulesError(
            f"rules.json: post_breakeven_trail {trail!r} not implemented "
            f"(supported: {sorted(_SUPPORTED_TRAILS)})"
        )

    fraction = float(_require(exit_, "partial_profit_fraction", "exit"))
    if not 0 < fraction < 1:
        raise RulesError(f"rules.json: partial_profit_fraction must be between 0 and 1, got {fraction}")

    rules = TrendJoinRules(
        strategy_name=raw["strategy_name"],
        direction=direction,
        trade_timeframe=raw["trade_timeframe"],
        index=_require(universe, "index", "universe_filters"),
        min_price_usd=float(_require(universe, "min_price_usd", "universe_filters")),
        d1_above_prior_day_high=bool(_require(daily, "D1_above_prior_day_high", "daily_filters")),
        d2_prior_close_above_sma200=bool(_require(daily, "D2_prior_close_above_sma200", "daily_filters")),
        d3_min_gap_pct=float(_require(daily, "D3_min_gap_pct_from_prior_close", "daily_filters")),
        i1_above_premarket_high=bool(_require(intraday, "I1_above_premarket_high", "intraday_filters")),
        i2_above_today_hod=bool(_require(intraday, "I2_above_today_hod", "intraday_filters")),
        i3_rvol_min=float(_require(intraday, "I3_rvol_min", "intraday_filters")),
        i3_rvol_lookback_days=int(_require(intraday, "I3_rvol_lookback_days", "intraday_filters")),
        earliest_entry_et=_parse_et(_require(timing, "earliest_entry_et", "time_filter"), "earliest_entry_et"),
        latest_entry_et=_parse_et(_require(timing, "latest_entry_et", "time_filter"), "latest_entry_et"),
        force_close_et=_parse_et(_require(timing, "force_close_et", "time_filter"), "force_close_et"),
        initial_stop_rule=stop_rule,
        partial_profit_trigger_r=float(_require(exit_, "partial_profit_trigger_R", "exit")),
        partial_profit_fraction=fraction,
        breakeven_trigger_r=float(_require(exit_, "breakeven_trigger_R", "exit")),
        post_breakeven_trail=trail,
        max_risk_per_trade_pct=float(_require(risk, "max_risk_per_trade_pct", "risk")),
        max_position_size_pct_of_portfolio=float(
            _require(risk, "max_position_size_pct_of_portfolio", "risk")
        ),
        max_concurrent_positions=int(_require(risk, "max_concurrent_positions", "risk")),
    )

    if not rules.earliest_entry_et < rules.latest_entry_et < rules.force_close_et:
        raise RulesError(
            "rules.json: time_filter must satisfy "
            "earliest_entry_et < latest_entry_et < force_close_et, got "
            f"{rules.earliest_entry_et} / {rules.latest_entry_et} / {rules.force_close_et}"
        )
    return rules


# --------------------------------------------------------------------------
# market snapshot the filters operate on
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Bar:
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Snapshot:
    """Everything the filters need about one symbol at one moment.

    The caller assembles this from IBKR data; the strategy never fetches.
    `bars_5m` is today's completed 5-minute bars, oldest first, and the last
    one is the bar being evaluated.
    """
    symbol: str
    price: float                    # last trade
    prior_day_high: float
    prior_day_close: float
    daily_closes: Sequence[float]   # most recent last, EXCLUDING today
    today_open: float
    premarket_high: float
    bars_5m: Sequence[Bar]
    volume_today: float
    avg_daily_volume: float         # over rules.i3_rvol_lookback_days
    session_fraction_elapsed: float  # 0..1 through the 09:30-16:00 session


@dataclass(frozen=True)
class FilterResult:
    passed: bool
    checks: dict          # name -> bool
    reasons: tuple        # human-readable failures, empty when passed

    def __bool__(self) -> bool:
        return self.passed


# --------------------------------------------------------------------------
# filters
# --------------------------------------------------------------------------


def sma(values: Sequence[float], length: int) -> float | None:
    if len(values) < length:
        return None
    return sum(values[-length:]) / length


def gap_pct(today_open: float, prior_close: float) -> float:
    if prior_close <= 0:
        return 0.0
    return (today_open - prior_close) / prior_close * 100.0


def relative_volume(snap: Snapshot) -> float:
    """Today's volume so far against what this symbol normally does by now.

    Approximated as today_volume / (avg_daily_volume * fraction_of_session
    elapsed). A true RVOL compares against the average *cumulative volume at
    this time of day*, which needs intraday history per day; this proxy uses
    daily bars and assumes volume accrues evenly. It therefore OVERSTATES
    RVOL early in the session, when real volume is front-loaded -- which is
    why the 10:05 earliest-entry rule matters, and why this number should
    not be trusted in the first 30 minutes.
    """
    if snap.avg_daily_volume <= 0 or snap.session_fraction_elapsed <= 0:
        return 0.0
    expected = snap.avg_daily_volume * snap.session_fraction_elapsed
    if expected <= 0:
        return 0.0
    return snap.volume_today / expected


def today_high_before_last_bar(bars: Sequence[Bar]) -> float | None:
    """High of day across every bar EXCEPT the one being evaluated.

    I2 asks whether price is breaking to a new high of day. Comparing the
    current price against a high-of-day that includes the current bar is
    self-referential and passes trivially, so the current bar is excluded.
    """
    if len(bars) < 2:
        return None
    return max(b.high for b in bars[:-1])


def evaluate_filters(snap: Snapshot, now_et: time, rules: TrendJoinRules) -> FilterResult:
    """Run every enabled filter. Returns which passed and why any failed."""
    checks: dict[str, bool] = {}
    reasons: list[str] = []

    def check(name: str, ok: bool, why: str) -> None:
        checks[name] = ok
        if not ok:
            reasons.append(why)

    check("min_price", snap.price >= rules.min_price_usd,
          f"price ${snap.price:.2f} below minimum ${rules.min_price_usd:.2f}")

    check("time_window", rules.earliest_entry_et <= now_et <= rules.latest_entry_et,
          f"{now_et:%H:%M} ET outside entry window "
          f"{rules.earliest_entry_et:%H:%M}-{rules.latest_entry_et:%H:%M}")

    if rules.d1_above_prior_day_high:
        check("D1_above_prior_day_high", snap.price > snap.prior_day_high,
              f"price ${snap.price:.2f} not above prior day high ${snap.prior_day_high:.2f}")

    if rules.d2_prior_close_above_sma200:
        sma200 = sma(snap.daily_closes, 200)
        if sma200 is None:
            check("D2_prior_close_above_sma200", False,
                  f"only {len(snap.daily_closes)} daily closes, need 200 for SMA200")
        else:
            check("D2_prior_close_above_sma200", snap.prior_day_close > sma200,
                  f"prior close ${snap.prior_day_close:.2f} not above SMA200 ${sma200:.2f}")

    gap = gap_pct(snap.today_open, snap.prior_day_close)
    check("D3_min_gap_pct", gap >= rules.d3_min_gap_pct,
          f"gap {gap:.2f}% below minimum {rules.d3_min_gap_pct:.2f}%")

    if rules.i1_above_premarket_high:
        check("I1_above_premarket_high", snap.price > snap.premarket_high,
              f"price ${snap.price:.2f} not above premarket high ${snap.premarket_high:.2f}")

    if rules.i2_above_today_hod:
        hod = today_high_before_last_bar(snap.bars_5m)
        if hod is None:
            check("I2_above_today_hod", False, "not enough 5m bars to establish a high of day")
        else:
            check("I2_above_today_hod", snap.price > hod,
                  f"price ${snap.price:.2f} not above today's high ${hod:.2f}")

    rvol = relative_volume(snap)
    check("I3_rvol_min", rvol >= rules.i3_rvol_min,
          f"RVOL {rvol:.2f} below minimum {rules.i3_rvol_min:.2f}")

    return FilterResult(passed=all(checks.values()), checks=checks, reasons=tuple(reasons))


# --------------------------------------------------------------------------
# entry plan / sizing
# --------------------------------------------------------------------------


class EntryRejected(Exception):
    """The setup passed the filters but cannot be sized within the risk rules."""


@dataclass(frozen=True)
class EntryPlan:
    symbol: str
    entry: float
    stop: float
    quantity: float
    position_value: float
    risk_dollars: float
    risk_per_share: float
    binding_cap: str


def initial_stop(snap: Snapshot, rules: TrendJoinRules) -> float:
    """`lod_minus_1pct`: 1% below the session low, including the current bar."""
    if not snap.bars_5m:
        raise EntryRejected(f"{snap.symbol}: no 5m bars, cannot compute low of day")
    lod = min(b.low for b in snap.bars_5m)
    return lod * 0.99


def build_entry(
    snap: Snapshot,
    rules: TrendJoinRules,
    equity: float,
    open_position_count: int,
    fractional: bool = True,
) -> EntryPlan:
    """Size a long entry under rules.json's risk block.

    Two caps, tighter wins:
      * max_risk_per_trade_pct of equity, divided by the per-share stop
        distance -- how many shares can be held before a stop-out costs more
        than the risk budget
      * max_position_size_pct_of_portfolio of equity as raw notional
    """
    if open_position_count >= rules.max_concurrent_positions:
        raise EntryRejected(
            f"{snap.symbol}: already at max_concurrent_positions "
            f"({open_position_count}/{rules.max_concurrent_positions})"
        )
    if equity <= 0:
        raise EntryRejected(f"{snap.symbol}: equity must be positive, got {equity}")

    entry = snap.price
    stop = initial_stop(snap, rules)
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        raise EntryRejected(
            f"{snap.symbol}: stop ${stop:.2f} is not below entry ${entry:.2f} "
            "(low of day is above the current price)"
        )

    risk_budget = equity * rules.max_risk_per_trade_pct / 100.0
    qty_by_risk = risk_budget / risk_per_share
    notional_cap = equity * rules.max_position_size_pct_of_portfolio / 100.0
    qty_by_notional = notional_cap / entry

    binding_cap = "risk_per_trade" if qty_by_risk <= qty_by_notional else "position_size"
    quantity = min(qty_by_risk, qty_by_notional)
    if not fractional:
        quantity = float(int(quantity))

    min_qty = 1e-6 if fractional else 1.0
    if quantity < min_qty:
        raise EntryRejected(
            f"{snap.symbol}: position too small — risk budget ${risk_budget:.2f} "
            f"and notional cap ${notional_cap:.2f} can't buy "
            f"{'a fractional share' if fractional else '1 share'} at ${entry:.2f}"
        )

    return EntryPlan(
        symbol=snap.symbol,
        entry=entry,
        stop=stop,
        quantity=quantity,
        position_value=quantity * entry,
        risk_dollars=quantity * risk_per_share,
        risk_per_share=risk_per_share,
        binding_cap=binding_cap,
    )


# --------------------------------------------------------------------------
# exits
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LivePosition:
    symbol: str
    entry: float
    stop: float               # current resting stop
    quantity: float
    initial_risk_per_share: float
    partial_taken: bool = False


def r_multiple(pos: LivePosition, price: float) -> float:
    if pos.initial_risk_per_share <= 0:
        return 0.0
    return (price - pos.entry) / pos.initial_risk_per_share


def swing_low_5m(bars: Sequence[Bar], left: int = 2, right: int = 2) -> float | None:
    """Most recent CONFIRMED 5m swing low: a bar whose low is strictly below
    the `left` bars before it and the `right` bars after it.

    Requiring `right` bars after means a swing low is only recognised ~10
    minutes late. That lag is the point -- an unconfirmed low is just the
    current bar, and trailing to it would stop the position out on noise.
    """
    n = len(bars)
    for i in range(n - right - 1, left - 1, -1):
        low = bars[i].low
        if all(low < bars[j].low for j in range(i - left, i)) and \
           all(low < bars[j].low for j in range(i + 1, i + right + 1)):
            return low
    return None


@dataclass(frozen=True)
class ExitAction:
    kind: str          # "partial" | "move_stop" | "force_close" | "stop_hit"
    reason: str
    quantity: float | None = None
    new_stop: float | None = None


def evaluate_exits(
    pos: LivePosition,
    snap: Snapshot,
    now_et: time,
    rules: TrendJoinRules,
) -> list[ExitAction]:
    """All exit actions due right now, most urgent first.

    Order matters: a stop hit and a force-close both end the trade and are
    checked before anything that would modify it.
    """
    actions: list[ExitAction] = []
    price = snap.price

    if price <= pos.stop:
        return [ExitAction("stop_hit", f"price ${price:.2f} at or below stop ${pos.stop:.2f}",
                           quantity=pos.quantity)]

    if now_et >= rules.force_close_et:
        return [ExitAction("force_close",
                           f"{now_et:%H:%M} ET at or past force_close {rules.force_close_et:%H:%M}",
                           quantity=pos.quantity)]

    r = r_multiple(pos, price)

    if not pos.partial_taken and r >= rules.partial_profit_trigger_r:
        qty = pos.quantity * rules.partial_profit_fraction
        actions.append(ExitAction(
            "partial",
            f"{r:.2f}R >= {rules.partial_profit_trigger_r}R trigger",
            quantity=qty,
        ))

    if r >= rules.breakeven_trigger_r and pos.stop < pos.entry:
        actions.append(ExitAction(
            "move_stop",
            f"{r:.2f}R >= {rules.breakeven_trigger_r}R, stop to breakeven",
            new_stop=pos.entry,
        ))
    elif pos.stop >= pos.entry:
        # Post-breakeven: trail behind confirmed swing lows, never loosening.
        swing = swing_low_5m(snap.bars_5m)
        if swing is not None and swing > pos.stop:
            actions.append(ExitAction(
                "move_stop",
                f"trailing to confirmed 5m swing low ${swing:.2f}",
                new_stop=swing,
            ))

    return actions
