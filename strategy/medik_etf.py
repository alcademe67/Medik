"""MEDIK ETF ACTIVE — intraday momentum on a fixed ETF universe.

Pure decision logic: scoring, ranking, correlation-aware exposure, position
sizing, and session risk controls. Nothing here imports ib_async, reads the
clock, or places an order — the caller supplies market data and account
state, which is what makes the whole strategy testable offline.

The runner is examples/medik_etf_live.py.

WHY EVERY TRADE IS A BUY
------------------------
The account is a TFSA and cannot short. That is not a limitation for this
strategy: the universe contains inverse ETFs (SQQQ, SOXS, TZA, LABD, FAZ,
ERY), and BUYING an inverse ETF is a long position that expresses a bearish
view. So a bearish setup on semiconductors is traded by buying SOXS, not by
shorting SOXL. Each instrument is scored on its OWN price action — the
inverse funds are never traded as a mechanical inversion of their bull twin.

LEVERAGE IS A SIZING INPUT, NOT A SIGNAL
-----------------------------------------
Most of this universe is 2x or 3x daily-reset. Those funds decay in chop and
can lose value while the underlying index goes nowhere. They are permitted
here because this is an INTRADAY strategy — the decay that makes them unfit
for buy-and-hold matters much less over hours — but notional is scaled DOWN
by leverage so a 3x fund never carries three times the dollar risk of a 1x
one. Higher volatility must never mean a larger position.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from strategy.medik_mtf import OHLCV, atr, ema, rsi

ETF_UNIVERSE = [
    "SNXX",
    "TQQQ", "SQQQ",
    "SOXL", "SOXS",
    "TNA", "TZA",
    "LABU", "LABD",
    "FAS", "FAZ",
    "ERX", "ERY",
    "QQQ", "SPY", "IWM",
    "SMH", "XLK", "XLF", "XLE",
]


@dataclass(frozen=True)
class ETFProfile:
    """Static facts about an instrument that affect sizing and correlation.

    leverage   -- absolute daily reset multiple (1.0 = unleveraged)
    ndx_beta   -- SIGNED sensitivity to the Nasdaq-100. Negative for inverse
                  funds, which is what lets an inverse position OFFSET the
                  QQQ core holding rather than add to it.
    group      -- coarse sector bucket, used to avoid stacking correlated
                  names in the same session.
    """
    symbol: str
    leverage: float
    ndx_beta: float
    group: str


ETF_PROFILES: dict[str, ETFProfile] = {
    # single-stock leveraged — highest idiosyncratic risk in the universe
    "SNXX": ETFProfile("SNXX", 2.0, 1.6, "single_stock_tech"),
    # Nasdaq-100
    "TQQQ": ETFProfile("TQQQ", 3.0, 3.0, "nasdaq"),
    "SQQQ": ETFProfile("SQQQ", 3.0, -3.0, "nasdaq"),
    "QQQ": ETFProfile("QQQ", 1.0, 1.0, "nasdaq"),
    # semiconductors — very high Nasdaq correlation
    "SOXL": ETFProfile("SOXL", 3.0, 3.3, "semis"),
    "SOXS": ETFProfile("SOXS", 3.0, -3.3, "semis"),
    "SMH": ETFProfile("SMH", 1.0, 1.1, "semis"),
    # broad tech
    "XLK": ETFProfile("XLK", 1.0, 1.0, "tech"),
    # small caps
    "TNA": ETFProfile("TNA", 3.0, 2.1, "smallcap"),
    "TZA": ETFProfile("TZA", 3.0, -2.1, "smallcap"),
    "IWM": ETFProfile("IWM", 1.0, 0.7, "smallcap"),
    # broad market
    "SPY": ETFProfile("SPY", 1.0, 0.85, "broad"),
    # biotech — low Nasdaq beta
    "LABU": ETFProfile("LABU", 3.0, 0.9, "biotech"),
    "LABD": ETFProfile("LABD", 3.0, -0.9, "biotech"),
    # financials
    "FAS": ETFProfile("FAS", 3.0, 0.6, "financials"),
    "FAZ": ETFProfile("FAZ", 3.0, -0.6, "financials"),
    "XLF": ETFProfile("XLF", 1.0, 0.2, "financials"),
    # energy — effectively uncorrelated to the Nasdaq
    "ERX": ETFProfile("ERX", 2.0, 0.1, "energy"),
    "ERY": ETFProfile("ERY", 2.0, -0.1, "energy"),
    "XLE": ETFProfile("XLE", 1.0, 0.05, "energy"),
}


def profile_for(symbol: str) -> ETFProfile:
    """Unknown symbols get the most conservative assumption, never a guess
    that would permit a larger position."""
    return ETF_PROFILES.get(symbol, ETFProfile(symbol, 3.0, 1.0, "unknown"))


# --------------------------------------------------------------- thresholds

MIN_SCORE = 75.0
RSI_MIN, RSI_MAX = 50.0, 70.0
MIN_RVOL = 1.5
MAX_SPREAD_PCT = 0.30          # bid/ask spread as % of price
MIN_DOLLAR_VOLUME = 5_000_000  # per session
MAX_EXTENSION_ATR = 1.5        # how far above VWAP price may be and still qualify

RISK_PCT_DEFAULT = 0.5
RISK_PCT_MAX = 1.0
ATR_STOP_MULT = 1.5
MIN_REWARD_RISK = 1.5

MAX_NOTIONAL_PCT = 25.0        # of equity, before leverage scaling
MAX_NDX_EXPOSURE_PCT = 90.0    # |net Nasdaq-beta-weighted exposure| cap

MAX_ACTIVE_POSITIONS = 1
MAX_TRADES_PER_SESSION = 3
MAX_DAILY_LOSS_PCT = 2.0
OPEN_DELAY_MIN = 15
CLOSE_BUFFER_MIN = 30


# ------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class ETFSnapshot:
    """Everything needed to score one ETF at one moment."""
    symbol: str
    price: float
    bid: float
    ask: float
    bars_5m: Sequence[OHLCV]      # today's session, oldest first, CLOSED bars
    bars_15m: Sequence[OHLCV]     # recent 15m bars, CLOSED
    session_dollar_volume: float


@dataclass(frozen=True)
class CandidateScore:
    symbol: str
    score: float
    signal: str                   # "TRADE" | "WATCH" | "REJECT"
    price: float
    rsi: float
    rvol: float
    atr: float
    vwap: float
    spread_pct: float
    breakout: bool
    pullback_reclaim: bool
    trend_15m: str
    reasons: tuple = ()
    rejections: tuple = ()

    @property
    def momentum(self) -> float:
        """Distance above VWAP in ATRs — used only for ranking ties."""
        return (self.price - self.vwap) / self.atr if self.atr > 0 else 0.0


# ---------------------------------------------------------------- indicators


def vwap(bars: Sequence[OHLCV]) -> float:
    """Volume-weighted average price over the bars given.

    The caller passes ONE session's bars; VWAP resets daily and computing it
    across a multi-day window produces a number that means nothing.
    """
    total_pv = total_v = 0.0
    for b in bars:
        typical = (b.high + b.low + b.close) / 3.0
        total_pv += typical * b.volume
        total_v += b.volume
    if total_v <= 0:
        return bars[-1].close if bars else 0.0
    return total_pv / total_v


def relative_volume(bars: Sequence[OHLCV], lookback: int = 20) -> float:
    """Current bar's volume over the average of the `lookback` bars before it.

    The current bar is excluded from its own baseline — including it drags
    the average toward the value being tested.
    """
    if len(bars) < lookback + 1:
        return 1.0
    window = bars[-(lookback + 1):-1]
    avg = sum(b.volume for b in window) / len(window)
    return bars[-1].volume / avg if avg > 0 else 1.0


def trend_15m(bars: Sequence[OHLCV]) -> str:
    """Primary regime filter: EMA 9/21 plus a breakdown check."""
    if len(bars) < 25:
        return "NEUTRAL"
    closes = [b.close for b in bars]
    e9, e21 = ema(closes, 9)[-1], ema(closes, 21)[-1]
    close = closes[-1]
    if e9 > e21 and close > e21:
        return "BULLISH"
    if e9 < e21 and close < e21:
        return "BEARISH"
    return "NEUTRAL"


def breakout_level(bars: Sequence[OHLCV], lookback: int = 12) -> float | None:
    """Highest high of the `lookback` bars BEFORE the current one."""
    if len(bars) < lookback + 1:
        return None
    return max(b.high for b in bars[-(lookback + 1):-1])


def is_pullback_reclaim(bars: Sequence[OHLCV]) -> bool:
    """breakout -> controlled pullback -> continuation.

    Preferred over a raw breakout because it enters after the first push has
    been digested, rather than chasing an extended candle. Requires: price
    pulled back to or below the 9-EMA within the last few bars, and the
    current bar has reclaimed it while making a higher low.
    """
    if len(bars) < 12:
        return False
    closes = [b.close for b in bars]
    e9 = ema(closes, 9)
    touched = any(bars[i].low <= e9[i] for i in range(len(bars) - 5, len(bars) - 1))
    reclaimed = closes[-1] > e9[-1]
    higher_low = bars[-1].low > min(b.low for b in bars[-5:-1])
    return touched and reclaimed and higher_low


# ------------------------------------------------------------------ scoring


def score_candidate(snap: ETFSnapshot) -> CandidateScore:
    """Score one ETF out of 100. Long-only: a BUY of this instrument."""
    reasons: list[str] = []
    rejections: list[str] = []
    score = 0.0

    bars5 = list(snap.bars_5m)
    if len(bars5) < 25 or len(snap.bars_15m) < 25:
        return CandidateScore(snap.symbol, 0.0, "REJECT", snap.price, 0.0, 0.0,
                              0.0, 0.0, 0.0, False, False, "NEUTRAL",
                              rejections=("insufficient bars",))

    closes = [b.close for b in bars5]
    e9, e21 = ema(closes, 9)[-1], ema(closes, 21)[-1]
    r = rsi(closes, 14)[-1]
    a = atr(bars5, 14)[-1]
    vw = vwap(bars5)
    rvol = relative_volume(bars5)
    price = snap.price
    t15 = trend_15m(snap.bars_15m)
    spread_pct = ((snap.ask - snap.bid) / price * 100.0) if price > 0 else 999.0
    level = breakout_level(bars5)
    broke = level is not None and price > level
    reclaim = is_pullback_reclaim(bars5)

    # 1. 15-minute regime (20) — the primary filter
    if t15 == "BULLISH":
        score += 20
        reasons.append("15m bullish")
    else:
        rejections.append(f"15m trend {t15}")

    # 2. 5-minute EMA alignment (15)
    if e9 > e21:
        score += 15
        reasons.append("5m EMA 9>21")
    else:
        rejections.append("5m EMA 9<=21")

    # 3. VWAP (15)
    if price > vw:
        score += 15
        reasons.append("above VWAP")
    else:
        rejections.append("below VWAP")

    # 4. RSI band (12) — a band, not a floor: RSI above the band is extended,
    #    and buying because RSI is high is precisely the chase to avoid.
    if RSI_MIN <= r <= RSI_MAX:
        score += 12
        reasons.append(f"RSI {r:.0f}")
    else:
        rejections.append(f"RSI {r:.0f} outside {RSI_MIN:.0f}-{RSI_MAX:.0f}")

    # 5. relative volume (15)
    if rvol >= MIN_RVOL:
        score += 15
        reasons.append(f"RVOL {rvol:.1f}")
    else:
        rejections.append(f"RVOL {rvol:.2f} below {MIN_RVOL}")

    # 6. breakout OR controlled pullback/reclaim (15) — reclaim preferred
    if reclaim:
        score += 15
        reasons.append("pullback reclaim")
    elif broke:
        score += 12
        reasons.append("breakout")
    else:
        rejections.append("no breakout or reclaim")

    # 7. spread (4)
    if spread_pct <= MAX_SPREAD_PCT:
        score += 4
        reasons.append(f"spread {spread_pct:.2f}%")
    else:
        rejections.append(f"spread {spread_pct:.2f}% above {MAX_SPREAD_PCT}%")

    # 8. liquidity (4)
    if snap.session_dollar_volume >= MIN_DOLLAR_VOLUME:
        score += 4
        reasons.append("liquid")
    else:
        rejections.append(f"dollar volume ${snap.session_dollar_volume:,.0f} below "
                          f"${MIN_DOLLAR_VOLUME:,}")

    # anti-chase: an extended candle disqualifies regardless of score
    extended = a > 0 and (price - vw) / a > MAX_EXTENSION_ATR
    if extended:
        rejections.append(f"extended {(price - vw) / a:.1f} ATR above VWAP")

    # HARD GATES. The 15-minute regime is the primary trend filter, so a
    # non-bullish 15m must veto the trade outright rather than merely cost
    # points -- otherwise a candidate can score 80 on the other components and
    # trade straight against the prevailing trend. Same for an extended entry.
    vetoed = extended or t15 != "BULLISH"

    if score >= MIN_SCORE and not vetoed:
        signal = "TRADE"
    elif score >= 60:
        signal = "WATCH"
    else:
        signal = "REJECT"

    return CandidateScore(
        symbol=snap.symbol, score=score, signal=signal, price=price, rsi=r,
        rvol=rvol, atr=a, vwap=vw, spread_pct=spread_pct, breakout=broke,
        pullback_reclaim=reclaim, trend_15m=t15,
        reasons=tuple(reasons), rejections=tuple(rejections),
    )


def rank_candidates(scores: Sequence[CandidateScore]) -> list[CandidateScore]:
    """Rank by the spec's priority order. Only TRADE candidates are ranked."""
    tradeable = [s for s in scores if s.signal == "TRADE"]
    return sorted(
        tradeable,
        key=lambda s: (
            s.score,                       # 1. strategy score
            s.rvol,                        # 2. relative volume
            s.momentum,                    # 3. momentum
            min(s.atr / s.price, 0.05) if s.price else 0.0,   # 4. usable volatility
            -s.spread_pct,                 # 6. spread quality
            1.0 if s.pullback_reclaim else 0.0,               # 7. breakout quality
        ),
        reverse=True,
    )


# ------------------------------------------------------- portfolio exposure


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: float
    market_value: float


@dataclass(frozen=True)
class PortfolioState:
    net_liquidation: float
    available_cash: float
    positions: tuple = ()          # tuple[Position, ...]
    open_order_count: int = 0

    @property
    def gross_exposure(self) -> float:
        return sum(abs(p.market_value) for p in self.positions)

    @property
    def active_position_count(self) -> int:
        return sum(1 for p in self.positions if p.quantity)


def ndx_exposure(positions: Sequence[Position]) -> float:
    """Net Nasdaq-beta-weighted dollar exposure across all holdings.

    This is the number that makes QQQ reduce the room for TQQQ/SNXX/XLK/SMH.
    A $200 QQQ position contributes +$200; adding $100 of TQQQ contributes
    +$300 on top, so the pair is $500 of Nasdaq risk on a $290 account even
    though the notional is $300. Inverse funds contribute NEGATIVE exposure
    and genuinely offset — that is why they are scored on their own merits
    rather than excluded.
    """
    return sum(p.market_value * profile_for(p.symbol).ndx_beta for p in positions)


def ndx_headroom(state: PortfolioState, symbol: str) -> float:
    """Dollars of `symbol` that can be added before the Nasdaq exposure cap.

    Returns infinity for instruments with negligible Nasdaq beta, and can
    return a LARGER allowance for an inverse fund when existing exposure is
    positive — buying SQQQ against a long QQQ book reduces net risk.
    """
    beta = profile_for(symbol).ndx_beta
    if abs(beta) < 0.15:
        return math.inf
    cap = state.net_liquidation * MAX_NDX_EXPOSURE_PCT / 100.0
    current = ndx_exposure(state.positions)
    room = (cap - current) if beta > 0 else (cap + current)
    return max(0.0, room / abs(beta))


# --------------------------------------------------------------- sizing


class SizingRejected(Exception):
    """The setup is valid but cannot be sized within the risk rules."""


@dataclass(frozen=True)
class SizedTrade:
    symbol: str
    quantity: int                  # WHOLE shares, always
    entry: float
    stop: float
    target: float
    risk_dollars: float
    notional: float
    reward_risk: float
    binding_cap: str


def size_trade(
    candidate: CandidateScore,
    state: PortfolioState,
    risk_pct: float = RISK_PCT_DEFAULT,
) -> SizedTrade:
    """Size a long entry in whole shares, or raise SizingRejected.

    Caps applied, tightest wins:
      * risk_pct of net liquidation, divided by the ATR stop distance
      * MAX_NOTIONAL_PCT of equity, scaled DOWN by the fund's leverage so a
        3x product never carries 3x the dollar exposure of a 1x one
      * available cash
      * Nasdaq-beta headroom, which is what QQQ consumes
    """
    if risk_pct > RISK_PCT_MAX:
        raise SizingRejected(
            f"risk_pct {risk_pct} exceeds the {RISK_PCT_MAX}% hard ceiling")
    if state.net_liquidation <= 0:
        raise SizingRejected("net liquidation must be positive")
    if candidate.atr <= 0:
        raise SizingRejected(f"{candidate.symbol}: ATR is zero, cannot set a stop")

    entry = candidate.price
    stop = entry - candidate.atr * ATR_STOP_MULT
    if stop <= 0 or stop >= entry:
        raise SizingRejected(f"{candidate.symbol}: invalid stop ${stop:.2f} vs entry ${entry:.2f}")
    stop_distance = entry - stop
    target = entry + stop_distance * MIN_REWARD_RISK

    prof = profile_for(candidate.symbol)
    risk_budget = state.net_liquidation * risk_pct / 100.0
    qty_by_risk = risk_budget / stop_distance

    # leverage-scaled notional: a 3x fund gets a third of the notional room
    notional_cap = state.net_liquidation * MAX_NOTIONAL_PCT / 100.0 / prof.leverage
    qty_by_notional = notional_cap / entry
    qty_by_cash = state.available_cash / entry
    qty_by_ndx = ndx_headroom(state, candidate.symbol) / entry

    caps = {
        "risk_per_trade": qty_by_risk,
        "position_notional": qty_by_notional,
        "available_cash": qty_by_cash,
        "ndx_exposure": qty_by_ndx,
    }
    binding_cap = min(caps, key=caps.get)
    raw_qty = caps[binding_cap]

    # WHOLE SHARES ONLY. Bracket legs are unreliable on fractional quantities,
    # and rounding UP would silently create an oversized position, so this
    # floors and refuses below one share.
    quantity = int(math.floor(raw_qty))
    if quantity < 1:
        raise SizingRejected(
            f"SKIP: quantity below 1 whole share "
            f"({raw_qty:.4f} at ${entry:.2f}, binding cap: {binding_cap})"
        )

    return SizedTrade(
        symbol=candidate.symbol, quantity=quantity, entry=entry, stop=stop,
        target=target, risk_dollars=quantity * stop_distance,
        notional=quantity * entry, reward_risk=MIN_REWARD_RISK,
        binding_cap=binding_cap,
    )


# --------------------------------------------------------- session controls


@dataclass
class SessionControls:
    equity_start_of_session: float
    trades_completed: int = 0
    entries_disabled: bool = False
    disabled_reason: str = ""
    execution_errors: int = 0

    def disable(self, reason: str) -> None:
        self.entries_disabled = True
        self.disabled_reason = reason


def minutes_since_open(now_minutes: int, open_minutes: int = 9 * 60 + 30) -> int:
    return now_minutes - open_minutes


def within_trading_window(
    now_minutes: int,
    open_minutes: int = 9 * 60 + 30,
    close_minutes: int = 16 * 60,
    open_delay: int = OPEN_DELAY_MIN,
    close_buffer: int = CLOSE_BUFFER_MIN,
) -> tuple[bool, str]:
    """New ENTRIES are allowed only inside the window. Existing positions are
    managed by their brackets regardless — this gates entries only."""
    if now_minutes < open_minutes + open_delay:
        return False, (f"within {open_delay}min opening delay "
                       f"(opens {open_minutes // 60:02d}:{open_minutes % 60:02d})")
    if now_minutes >= close_minutes - close_buffer:
        return False, f"within {close_buffer}min of the close"
    return True, ""


def check_can_enter(
    state: PortfolioState,
    controls: SessionControls,
    now_minutes: int,
) -> tuple[bool, str]:
    """Every gate between a ranked candidate and an order. First breach wins."""
    if controls.entries_disabled:
        return False, f"entries disabled: {controls.disabled_reason}"

    if state.active_position_count >= MAX_ACTIVE_POSITIONS:
        return False, (f"already holding {state.active_position_count} position(s), "
                       f"max {MAX_ACTIVE_POSITIONS}")

    if controls.trades_completed >= MAX_TRADES_PER_SESSION:
        return False, (f"session trade cap reached "
                       f"({controls.trades_completed}/{MAX_TRADES_PER_SESSION})")

    if controls.equity_start_of_session > 0:
        loss_pct = ((controls.equity_start_of_session - state.net_liquidation)
                    / controls.equity_start_of_session * 100.0)
        if loss_pct >= MAX_DAILY_LOSS_PCT:
            return False, (f"daily loss limit hit: -{loss_pct:.2f}% "
                           f"(limit {MAX_DAILY_LOSS_PCT}%)")

    ok, why = within_trading_window(now_minutes)
    if not ok:
        return False, why

    if state.open_order_count > 0:
        return False, f"{state.open_order_count} open order(s) outstanding"

    return True, ""
