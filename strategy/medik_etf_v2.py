"""MEDIK ETF v2 — cost-aware intraday ETF momentum.

v1 asked "is this a good setup?". v2 asks the question that actually decides
profitability: "after commission, spread and slippage, is there anything
left?" Everything below follows from that.

WHAT CHANGED FROM v1
    * Universe trimmed to liquid, non-inverse funds. SQQQ/SOXS removed --
      not because inverse funds are wrong in principle, but because a
      bearish leg doubles the trade count for the same capital, and trade
      count is what costs money.
    * A HARD net-edge gate: the target must clear the round-trip cost by
      MIN_EDGE_MULTIPLE or the trade does not happen, regardless of score.
      This is the rule v1 lacked.
    * Stronger confirmation: higher score floor, and a breakout alone is no
      longer enough -- a pullback/reclaim is required.
    * Faster failure exit: leave when momentum dies rather than waiting for
      the ATR stop, because a full stop-out costs risk PLUS the round trip.
    * Longer re-entry cooldown, halving repeat entries on the same symbol.

THE ARITHMETIC THAT MOTIVATED v2 -- read before enabling anything
    The risk rule caps a 1.5R winner at 1.5 x 0.5% = 0.75% of equity. At
    $290 that is $2.17 gross, against ~$1.44 of round-trip cost. So a
    perfect trade nets ~$0.73 while a loser costs ~$2.89, and the
    break-even win rate is ~80%. A 1.5R strategy that wins 80% of the time
    does not exist.

    minimum_viable_equity() computes where this stops being true. Until the
    account is above it, the honest expectation for this strategy is a slow
    loss, and the net-edge gate will correctly refuse nearly every trade.
"""
from __future__ import annotations

from dataclasses import dataclass

from strategy.medik_etf import (
    ATR_STOP_MULT,
    MIN_REWARD_RISK,
    RISK_PCT_DEFAULT,
    CandidateScore,
    PortfolioState,
    profile_for,
)

# Liquid, non-inverse. Inverse funds are removed for cost reasons, not
# directional ones -- see the module docstring.
V2_UNIVERSE = [
    "SNXX", "TQQQ", "SOXL",          # leveraged momentum
    "QQQ", "SPY", "IWM",             # broad market
    "SMH", "XLK", "XLF", "XLE",      # liquid sectors
    "DIA", "VTI",                    # additional liquid broad funds
]

# ---- v2 thresholds (stricter than v1; never loosened to force a trade)
MIN_SCORE_V2 = 85.0            # v1 used 75
REQUIRE_RECLAIM = True         # a bare breakout no longer qualifies
MIN_EDGE_MULTIPLE = 1.5        # target must clear round-trip cost by 1.5x
REENTRY_COOLDOWN_SEC_V2 = 1800  # 30 minutes; v1 used 15
MOMENTUM_FAIL_BARS = 3         # bars below VWAP before bailing early

# ---- cost model (this account's real schedule)
PER_SHARE, MIN_COMMISSION, MAX_PCT = 0.005, 1.00, 0.01
SLIPPAGE_BPS = 2.0
SPREAD_BPS_BY_LEVERAGE = {1.0: 1.0, 2.0: 3.0, 3.0: 4.0}


def commission(shares: float, value: float) -> float:
    if shares <= 0 or value <= 0:
        return 0.0
    return min(max(PER_SHARE * shares, MIN_COMMISSION), MAX_PCT * value)


def spread_bps(symbol: str) -> float:
    return SPREAD_BPS_BY_LEVERAGE.get(profile_for(symbol).leverage, 4.0)


def round_trip_cost(symbol: str, quantity: int, entry: float, exit_price: float) -> float:
    """Total friction for one complete trade: both commissions, the spread
    crossed twice, and slippage."""
    entry_value, exit_value = quantity * entry, quantity * exit_price
    commissions = commission(quantity, entry_value) + commission(quantity, exit_value)
    spread = (entry_value + exit_value) * spread_bps(symbol) / 10_000.0
    slippage = (entry_value + exit_value) * SLIPPAGE_BPS / 10_000.0
    return commissions + spread + slippage


@dataclass(frozen=True)
class EdgeCheck:
    passes: bool
    expected_gross: float
    cost: float
    ratio: float
    required: float
    reason: str


def net_edge_check(
    symbol: str,
    quantity: int,
    entry: float,
    stop: float,
    target: float,
    min_multiple: float = MIN_EDGE_MULTIPLE,
) -> EdgeCheck:
    """Does the intended move cover its own friction with room to spare?

    This is v2's central rule. A setup can be technically perfect and still
    be a losing trade if the target is worth less than the cost of getting
    in and out -- which, at small position sizes, is the normal case rather
    than the exception.
    """
    expected_gross = quantity * (target - entry)
    cost = round_trip_cost(symbol, quantity, entry, target)
    ratio = expected_gross / cost if cost > 0 else 0.0
    passes = ratio >= min_multiple
    if passes:
        reason = f"target ${expected_gross:.2f} vs cost ${cost:.2f} = {ratio:.2f}x"
    else:
        reason = (f"NET EDGE FAIL: target ${expected_gross:.2f} only {ratio:.2f}x "
                  f"the ${cost:.2f} round-trip cost, need {min_multiple:.1f}x")
    return EdgeCheck(passes, expected_gross, cost, ratio, min_multiple, reason)


def qualifies_v2(candidate: CandidateScore) -> tuple[bool, str]:
    """v2's stricter technical gate, applied on top of v1's scoring."""
    if candidate.signal != "TRADE":
        return False, f"v1 signal is {candidate.signal}"
    if candidate.score < MIN_SCORE_V2:
        return False, f"score {candidate.score:.0f} below v2 floor {MIN_SCORE_V2:.0f}"
    if REQUIRE_RECLAIM and not candidate.pullback_reclaim:
        return False, "breakout without a pullback/reclaim — v2 requires the reclaim"
    return True, f"score {candidate.score:.0f}, reclaim confirmed"


def momentum_failed(closes_below_vwap: int) -> bool:
    """Exit early when the move stops working.

    Waiting for the ATR stop costs the full risk budget plus the round trip.
    Leaving when the thesis breaks costs the round trip and a fraction of
    the risk, which is the difference between a small loss and a full one.
    """
    return closes_below_vwap >= MOMENTUM_FAIL_BARS


def minimum_viable_equity(
    typical_price: float = 70.0,
    risk_pct: float = RISK_PCT_DEFAULT,
    min_multiple: float = MIN_EDGE_MULTIPLE,
    symbol: str = "TQQQ",
) -> float:
    """Smallest account where a 1.5R winner can clear costs by min_multiple.

    Derivation, independent of ATR and price:
        risk budget      = risk_frac * equity
        gross at target  = MIN_REWARD_RISK * risk budget
        requirement      = gross >= min_multiple * round_trip_cost
        therefore        equity >= min_multiple * cost
                                   / (risk_frac * MIN_REWARD_RISK)

    risk_pct is a PERCENT (0.5 means 0.5%), matching the rest of the repo.
    Cost depends on quantity, so this solves iteratively from one share up
    and returns the first equity at which the position is also affordable.
    """
    risk_frac = risk_pct / 100.0
    if risk_frac <= 0:
        return float("inf")
    for shares in range(1, 500):
        value = shares * typical_price
        cost = round_trip_cost(symbol, shares, typical_price, typical_price)
        equity = min_multiple * cost / (risk_frac * MIN_REWARD_RISK)
        # the position must also be affordable within a 90% allocation
        if value <= equity * 0.90:
            return equity
    return float("inf")
