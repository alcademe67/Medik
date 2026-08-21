"""Tests for the MEDIK ETF ACTIVE strategy (strategy/medik_etf.py).

Covers scoring, ranking, sizing, QQQ correlation handling, whole-share
enforcement, stop/target maths, the session risk controls, and the live-mode
gate. Pure logic — nothing here touches IBKR.
"""
from __future__ import annotations

import math

import pytest

from strategy.medik_etf import (
    ETF_PROFILES,
    ETF_UNIVERSE,
    MAX_DAILY_LOSS_PCT,
    MAX_TRADES_PER_SESSION,
    MIN_REWARD_RISK,
    RISK_PCT_MAX,
    ETFSnapshot,
    PortfolioState,
    Position,
    SessionControls,
    SizingRejected,
    breakout_level,
    check_can_enter,
    is_pullback_reclaim,
    ndx_exposure,
    ndx_headroom,
    profile_for,
    rank_candidates,
    relative_volume,
    score_candidate,
    size_trade,
    trend_15m,
    within_trading_window,
    vwap,
)
from strategy.medik_mtf import OHLCV

NOON = 12 * 60


def bars(closes, volume=200_000.0, spread=0.4):
    """Build bars with a small range around each close."""
    return [OHLCV(open=c, high=c + spread, low=c - spread, close=c, volume=volume)
            for c in closes]


def rising(n=40, start=100.0, step=0.25):
    return [start + i * step for i in range(n)]


def qualifying_bars(run_step=0.25, pullback=2.5, rng=1.0):
    """A realistic setup: morning run -> controlled pullback -> reclaim.

    NOT a straight line. A strictly rising series pins RSI at 100 and leaves
    price several ATR above VWAP, which this strategy deliberately REFUSES as
    an extended chase -- so a monotonic fixture tests the rejection path, not
    the acceptance path.
    """
    closes = [100 + i * run_step for i in range(18)]
    top = closes[-1]
    closes += [top - pullback * f for f in (0.2, 0.5, 0.8, 1.0, 1.05)]
    low = closes[-1]
    closes += [low + pullback * f for f in (0.25, 0.5, 0.7, 0.85)]
    vols = [200_000.0] * 18 + [140_000.0] * 5 + [180_000.0, 200_000.0, 260_000.0, 520_000.0]
    return [OHLCV(open=c, high=c + rng, low=c - rng, close=c, volume=v)
            for c, v in zip(closes, vols)]


def _snapshot(**kw) -> ETFSnapshot:
    """A snapshot engineered to score TRADE, so each test can break one part."""
    b5 = qualifying_bars()
    price = b5[-1].close
    base = dict(
        symbol="TQQQ", price=price, bid=price - 0.01, ask=price + 0.01,
        bars_5m=b5, bars_15m=bars(rising(40)),
        session_dollar_volume=50_000_000.0,
    )
    base.update(kw)
    return ETFSnapshot(**base)


# ------------------------------------------------------------------ universe


def test_every_universe_symbol_has_a_profile():
    for symbol in ETF_UNIVERSE:
        assert symbol in ETF_PROFILES, f"{symbol} missing a profile"


def test_unknown_symbols_get_the_conservative_default():
    p = profile_for("NOPE")
    assert p.leverage == 3.0        # assumes the worst, never permits more size


def test_inverse_funds_carry_negative_nasdaq_beta():
    for sym in ("SQQQ", "SOXS", "TZA", "LABD", "FAZ", "ERY"):
        assert ETF_PROFILES[sym].ndx_beta < 0, sym
    for sym in ("QQQ", "TQQQ", "SOXL", "SMH", "XLK"):
        assert ETF_PROFILES[sym].ndx_beta > 0, sym


# ---------------------------------------------------------------- indicators


def test_vwap_is_volume_weighted():
    b = [OHLCV(10, 10, 10, 10, 1.0), OHLCV(20, 20, 20, 20, 3.0)]
    assert vwap(b) == pytest.approx(17.5)       # (10*1 + 20*3) / 4


def test_vwap_handles_zero_volume():
    assert vwap([OHLCV(10, 10, 10, 10, 0.0)]) == 10.0
    assert vwap([]) == 0.0


def test_relative_volume_excludes_the_current_bar():
    b = bars([100.0] * 20, volume=100.0) + [OHLCV(100, 100, 100, 100, 300.0)]
    assert relative_volume(b) == pytest.approx(3.0)


def test_trend_15m_states():
    assert trend_15m(bars(rising(40))) == "BULLISH"
    assert trend_15m(bars(rising(40, start=140.0, step=-0.25))) == "BEARISH"
    assert trend_15m(bars([100.0] * 40)) == "NEUTRAL"


def test_breakout_level_excludes_the_current_bar():
    b = bars([100, 101, 102, 103])
    assert breakout_level(b, lookback=2) == pytest.approx(102.4)
    assert breakout_level(bars([100]), lookback=12) is None


def test_pullback_reclaim_needs_touch_reclaim_and_higher_low():
    flat = bars([100.0] * 20)
    assert is_pullback_reclaim(flat) is False
    assert is_pullback_reclaim(bars([100, 101])) is False


# ------------------------------------------------------------------- scoring


def test_clean_setup_scores_tradeable():
    cs = score_candidate(_snapshot())
    assert cs.signal == "TRADE", (cs.score, cs.rejections)
    assert cs.score >= 75


def test_insufficient_bars_rejects():
    cs = score_candidate(_snapshot(bars_5m=bars([100, 101])))
    assert cs.signal == "REJECT"
    assert "insufficient bars" in cs.rejections


def test_bearish_15m_regime_costs_the_biggest_component():
    good = score_candidate(_snapshot())
    bad = score_candidate(_snapshot(bars_15m=bars(rising(40, 140.0, -0.25))))
    assert good.score - bad.score == pytest.approx(20.0)
    assert bad.signal != "TRADE"


def test_wide_spread_is_penalised():
    cs = score_candidate(_snapshot(bid=108.0, ask=110.0))
    assert any("spread" in r for r in cs.rejections)


def test_thin_liquidity_is_penalised():
    cs = score_candidate(_snapshot(session_dollar_volume=1000.0))
    assert any("dollar volume" in r for r in cs.rejections)


def test_high_rsi_alone_does_not_qualify():
    """RSI is a band, not a floor. Buying because RSI is high is the chase."""
    b5 = bars([100 + i * 3.0 for i in range(40)])      # violent, RSI pinned high
    px = b5[-1].close
    cs = score_candidate(_snapshot(bars_5m=b5, price=px, bid=px - 0.01, ask=px + 0.01))
    assert cs.rsi > 70
    assert any("RSI" in r for r in cs.rejections)
    assert cs.signal != "TRADE"


def test_a_straight_line_rally_is_refused_as_extended():
    """Regression: the strategy must NOT buy a monotonic vertical move."""
    b5 = bars(rising(40))
    px = b5[-1].close
    cs = score_candidate(_snapshot(bars_5m=b5, price=px, bid=px - 0.01, ask=px + 0.01))
    assert cs.signal != "TRADE"
    assert any("extended" in r or "RSI" in r for r in cs.rejections)


def test_extended_candle_is_disqualified_regardless_of_score():
    b5 = bars(rising(40))
    b5[-1] = OHLCV(open=109.5, high=200.0, low=109.4, close=180.0, volume=900_000.0)
    cs = score_candidate(_snapshot(bars_5m=b5, price=180.0, bid=179.9, ask=180.1))
    assert cs.signal != "TRADE"
    assert any("extended" in r for r in cs.rejections)


# ------------------------------------------------------------------- ranking


def _cs(symbol, score, rvol=2.0, price=100.0, atr=1.0, vwap_=99.0, spread=0.05,
        reclaim=False):
    from strategy.medik_etf import CandidateScore
    return CandidateScore(symbol, score, "TRADE", price, 60.0, rvol, atr, vwap_,
                          spread, True, reclaim, "BULLISH")


def test_ranking_orders_by_score_then_rvol():
    ranked = rank_candidates([_cs("A", 80, rvol=2.0), _cs("B", 90, rvol=1.6),
                              _cs("C", 80, rvol=3.0)])
    assert [c.symbol for c in ranked] == ["B", "C", "A"]


def test_ranking_drops_non_tradeable():
    from strategy.medik_etf import CandidateScore
    watch = CandidateScore("W", 65, "WATCH", 100, 60, 2, 1, 99, 0.05, True, False, "BULLISH")
    ranked = rank_candidates([watch, _cs("A", 80)])
    assert [c.symbol for c in ranked] == ["A"]


def test_ranking_empty_when_nothing_qualifies():
    assert rank_candidates([]) == []


# ------------------------------------------------- QQQ / correlation handling


def test_qqq_position_creates_positive_nasdaq_exposure():
    state = PortfolioState(1000.0, 800.0, (Position("QQQ", 1, 200.0),))
    assert ndx_exposure(state.positions) == pytest.approx(200.0)


def test_leveraged_nasdaq_exposure_is_multiplied():
    state = PortfolioState(1000.0, 800.0, (Position("TQQQ", 1, 100.0),))
    assert ndx_exposure(state.positions) == pytest.approx(300.0)


def test_inverse_position_offsets_a_long_book():
    state = PortfolioState(1000.0, 800.0,
                           (Position("QQQ", 1, 300.0), Position("SQQQ", 1, 100.0)))
    assert ndx_exposure(state.positions) == pytest.approx(300.0 - 300.0)


def test_existing_qqq_reduces_headroom_for_correlated_etfs():
    """The spec's core requirement: QQQ exposure must shrink TQQQ room."""
    empty = PortfolioState(1000.0, 1000.0, ())
    heavy = PortfolioState(1000.0, 1000.0, (Position("QQQ", 1, 800.0),))
    assert ndx_headroom(heavy, "TQQQ") < ndx_headroom(empty, "TQQQ")
    assert ndx_headroom(heavy, "SNXX") < ndx_headroom(empty, "SNXX")


def test_headroom_is_unlimited_for_uncorrelated_names():
    state = PortfolioState(1000.0, 1000.0, (Position("QQQ", 1, 900.0),))
    assert ndx_headroom(state, "XLE") == math.inf


def test_inverse_headroom_grows_when_the_book_is_long():
    """Buying SQQQ against a long QQQ book REDUCES net risk, so it is allowed
    more room, not less."""
    state = PortfolioState(1000.0, 1000.0, (Position("QQQ", 1, 500.0),))
    assert ndx_headroom(state, "SQQQ") > ndx_headroom(PortfolioState(1000.0, 1000.0, ()), "SQQQ")


def test_headroom_never_negative():
    state = PortfolioState(1000.0, 1000.0, (Position("TQQQ", 1, 5000.0),))
    assert ndx_headroom(state, "QQQ") == 0.0


# ------------------------------------------------------------------- sizing


def test_stop_and_target_come_from_atr_and_meet_the_rr_floor():
    state = PortfolioState(100_000.0, 100_000.0, ())
    t = size_trade(_cs("QQQ", 80, price=100.0, atr=2.0), state)
    assert t.stop == pytest.approx(97.0)              # 100 - 1.5*2
    assert t.target == pytest.approx(104.5)           # 100 + 1.5*3
    assert (t.target - t.entry) / (t.entry - t.stop) == pytest.approx(MIN_REWARD_RISK)


def test_dollar_risk_respects_the_configured_percentage():
    state = PortfolioState(100_000.0, 100_000.0, ())
    t = size_trade(_cs("QQQ", 80, price=100.0, atr=2.0), state, risk_pct=0.5)
    assert t.risk_dollars <= 100_000.0 * 0.005 + 100.0   # +1 share of rounding
    assert t.binding_cap in ("risk_per_trade", "position_notional",
                             "available_cash", "ndx_exposure")


def test_risk_above_the_hard_ceiling_is_refused():
    state = PortfolioState(100_000.0, 100_000.0, ())
    with pytest.raises(SizingRejected, match="hard ceiling"):
        size_trade(_cs("QQQ", 80), state, risk_pct=RISK_PCT_MAX + 0.1)


def test_leverage_shrinks_notional_not_just_risk():
    """A 3x fund must not carry 3x the dollar exposure of a 1x one."""
    state = PortfolioState(100_000.0, 100_000.0, ())
    qqq = size_trade(_cs("QQQ", 80, price=100.0, atr=0.1), state)
    tqqq = size_trade(_cs("TQQQ", 80, price=100.0, atr=0.1), state)
    assert tqqq.notional < qqq.notional
    assert tqqq.notional == pytest.approx(qqq.notional / 3, rel=0.02)


def test_whole_shares_only():
    state = PortfolioState(100_000.0, 100_000.0, ())
    t = size_trade(_cs("QQQ", 80, price=137.0, atr=3.0), state)
    assert isinstance(t.quantity, int)
    assert t.quantity == int(t.quantity)


def test_below_one_share_is_skipped_not_rounded():
    """The exact failure the spec calls out: never silently round up."""
    state = PortfolioState(290.0, 87.0, ())
    with pytest.raises(SizingRejected, match="below 1 whole share"):
        size_trade(_cs("TQQQ", 80, price=500.0, atr=10.0), state)


def test_sizing_needs_a_positive_atr():
    state = PortfolioState(100_000.0, 100_000.0, ())
    with pytest.raises(SizingRejected, match="ATR is zero"):
        size_trade(_cs("QQQ", 80, atr=0.0), state)


def test_cash_can_be_the_binding_cap():
    state = PortfolioState(100_000.0, 250.0, ())
    t = size_trade(_cs("QQQ", 80, price=100.0, atr=0.5), state)
    assert t.binding_cap == "available_cash"
    assert t.notional <= 250.0


# --------------------------------------------------------- session controls


def _controls(**kw) -> SessionControls:
    base = dict(equity_start_of_session=1000.0)
    base.update(kw)
    return SessionControls(**base)


def test_clean_session_allows_entry():
    ok, why = check_can_enter(PortfolioState(1000.0, 1000.0, ()), _controls(), NOON)
    assert ok and why == ""


def test_one_active_position_maximum():
    state = PortfolioState(1000.0, 500.0, (Position("TQQQ", 5, 500.0),))
    ok, why = check_can_enter(state, _controls(), NOON)
    assert not ok and "already holding" in why


def test_session_trade_cap():
    state = PortfolioState(1000.0, 1000.0, ())
    ok, why = check_can_enter(state, _controls(trades_completed=MAX_TRADES_PER_SESSION), NOON)
    assert not ok and "session trade cap" in why


def test_daily_loss_limit():
    state = PortfolioState(1000.0 * (1 - MAX_DAILY_LOSS_PCT / 100), 900.0, ())
    ok, why = check_can_enter(state, _controls(), NOON)
    assert not ok and "daily loss limit" in why


def test_just_inside_the_daily_loss_limit_still_trades():
    state = PortfolioState(985.0, 900.0, ())      # -1.5%, limit is 2%
    assert check_can_enter(state, _controls(), NOON)[0] is True


def test_disabled_entries_block_everything():
    c = _controls()
    c.disable("bracket failure: stop leg is Cancelled")
    ok, why = check_can_enter(PortfolioState(1000.0, 1000.0, ()), c, NOON)
    assert not ok and "bracket failure" in why


def test_open_orders_block_a_new_entry():
    state = PortfolioState(1000.0, 1000.0, (), open_order_count=2)
    ok, why = check_can_enter(state, _controls(), NOON)
    assert not ok and "open order" in why


# ----------------------------------------------------------- market windows


def test_opening_delay_and_closing_buffer():
    assert within_trading_window(9 * 60 + 44)[0] is False   # 14 min after open
    assert within_trading_window(9 * 60 + 45)[0] is True    # 15 min after open
    assert within_trading_window(15 * 60 + 29)[0] is True   # 31 min before close
    assert within_trading_window(15 * 60 + 30)[0] is False  # 30 min before close


def test_window_explains_itself():
    assert "opening delay" in within_trading_window(9 * 60 + 31)[1]
    assert "close" in within_trading_window(15 * 60 + 45)[1]
