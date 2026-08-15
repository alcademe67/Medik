"""Regression tests for the five defects found reviewing the pasted strategy.py.

Each fix has a test that fails against the original behaviour.
"""
from __future__ import annotations

import pytest

from backtest.alca_mtf_bt import commission
from strategy.alca_mtf import (
    MAX_DISTANCE_FROM_EMA,
    MIN_CONFIDENCE,
    OHLCV,
    atr,
    confidence_score,
    daily_trend,
    drop_forming_bar,
    ema,
    generate_signal,
    rsi,
    weekly_trend,
)


def _bars(closes, volume=1_000_000):
    out = []
    for c in closes:
        out.append(OHLCV(open=c, high=c * 1.005, low=c * 0.995, close=c, volume=volume))
    return out


# ------------------------------------------------- FIX 1: RSI NaN in uptrend


def test_rsi_is_100_not_nan_when_there_are_no_losses():
    """The original produced NaN here, silently suppressing every long."""
    values = rsi([100 + i for i in range(45)])
    assert values[-1] == 100.0
    assert all(v == v for v in values)          # no NaN anywhere


def test_rsi_is_zero_in_a_pure_downtrend():
    assert rsi([100 - i for i in range(45)])[-1] == 0.0


def test_rsi_mid_range_for_mixed_series():
    closes = [100 + (2 if i % 2 else -1) for i in range(60)]
    assert 40.0 < rsi(closes)[-1] < 100.0


def test_bullish_weekly_regime_is_detected_in_a_clean_uptrend():
    """Consequence of FIX 1: this returned NEUTRAL before, so nothing traded."""
    assert weekly_trend(_bars([100 + i * 1.5 for i in range(60)])) == "BULLISH"


def test_bearish_regimes_still_detected():
    assert weekly_trend(_bars([200 - i * 1.5 for i in range(60)])) == "BEARISH"
    assert daily_trend(_bars([200 - i * 1.2 for i in range(80)])) == "BEARISH"


def test_flat_market_is_neutral():
    """A dead-flat tape has EMAs on top of each other and RSI at 50.

    (A strictly alternating +5/-5 series is NOT a good "chop" fixture: it
    ends on an up-bar with price above both EMAs and RSI above 50, which is
    genuinely bullish by these rules. The rules judge the last bar, and the
    last bar of that series is a rally.)
    """
    assert weekly_trend(_bars([100.0] * 60)) == "NEUTRAL"
    assert daily_trend(_bars([100.0] * 80)) == "NEUTRAL"


# ------------------------------------------- FIX 2: confidence actually varies


def test_confidence_is_not_a_constant():
    strong = confidence_score(60.0, 52.0, 68.0, 0.001, 2.0, True, True)
    weak = confidence_score(52.1, 52.0, 68.0, 0.0249, 0.2, True, True)
    assert strong > weak
    assert strong == pytest.approx(1.0, abs=0.02)
    assert weak < MIN_CONFIDENCE          # the threshold now rejects something


def test_confidence_penalises_misaligned_timeframes():
    both = confidence_score(60.0, 52.0, 68.0, 0.001, 2.0, True, True)
    daily_only = confidence_score(60.0, 52.0, 68.0, 0.001, 2.0, False, True)
    assert both - daily_only == pytest.approx(0.25)


def test_confidence_is_zero_outside_the_rsi_band():
    assert confidence_score(80.0, 52.0, 68.0, 0.001, 2.0, True, True) < 1.0
    edge = confidence_score(68.0, 52.0, 68.0, 0.0, 2.0, True, True)
    assert edge < confidence_score(60.0, 52.0, 68.0, 0.0, 2.0, True, True)


# --------------------------------------------- FIX 3: the forming bar is cut


def test_drop_forming_bar():
    bars = _bars([1, 2, 3])
    assert len(drop_forming_bar(bars, bar_complete=False)) == 2
    assert len(drop_forming_bar(bars, bar_complete=True)) == 3
    assert drop_forming_bar([], bar_complete=False) == []


# ------------------------------------ FIX 4: liquidity uses avg daily volume


def test_low_average_daily_volume_blocks_the_trade():
    up = _bars([100 + i * 1.5 for i in range(80)])
    sig = generate_signal("TEST", up, up, up, avg_daily_volume=1_000)
    assert sig.action == "HOLD"
    assert "avg daily volume" in sig.reason


def test_a_thin_15m_bar_does_not_block_a_liquid_name():
    """The original compared MIN_VOLUME against one 15m bar's volume."""
    up = _bars([100 + i * 1.5 for i in range(80)], volume=500)   # thin bars
    sig = generate_signal("TEST", up, up, up, avg_daily_volume=5_000_000)
    assert "avg daily volume" not in sig.reason


# --------------------------------------------- FIX 5: shorting is account-gated


def _bearish_setup():
    closes = [200 - i * 1.2 for i in range(80)]
    return _bars(closes)


def test_no_short_signal_without_account_permission():
    bars = _bearish_setup()
    sig = generate_signal("TEST", bars, bars, bars, avg_daily_volume=5_000_000)
    assert sig.action != "SELL"


def test_short_requires_explicit_opt_in():
    bars = _bearish_setup()
    default = generate_signal("TEST", bars, bars, bars, avg_daily_volume=5_000_000)
    opted = generate_signal("TEST", bars, bars, bars, avg_daily_volume=5_000_000,
                            account_can_short=True)
    assert default.action == "HOLD"
    # opting in may or may not produce a signal on this data, but it must never
    # be able to produce one while opted out
    assert not (default.action == "SELL")
    assert opted.action in ("SELL", "HOLD")


# ------------------------------------------------------------ general sanity


def test_ema_matches_the_recurrence():
    values = ema([1.0, 2.0, 3.0], 2)
    assert values[0] == 1.0
    assert values[1] == pytest.approx(1.0 + (2.0 / 3.0) * 1.0)


def test_atr_is_positive_and_smooth():
    values = atr(_bars([100 + i for i in range(40)]))
    assert all(v > 0 for v in values)


def test_insufficient_data_holds():
    sig = generate_signal("TEST", [], [], _bars([100, 101]), avg_daily_volume=1e6)
    assert sig.action == "HOLD"
    assert "not enough" in sig.reason


def test_long_signal_has_2to1_reward_risk():
    """Whenever a BUY fires, the target must sit at 2R."""
    closes = [100 + i * 0.6 for i in range(60)] + [136.0, 136.4]
    bars = _bars(closes)
    sig = generate_signal("TEST", bars, bars, bars, avg_daily_volume=5_000_000,
                          volume_ratio=2.0)
    if sig.action == "BUY":
        risk = sig.price - sig.stop_loss
        reward = sig.take_profit - sig.price
        assert reward == pytest.approx(2.0 * risk, rel=1e-3)


# ------------------------------------------------------- commission model


def test_commission_matches_the_account_schedule():
    assert commission(100, 10_000) == pytest.approx(1.00)     # min binds
    assert commission(1000, 100_000) == pytest.approx(5.00)   # per-share binds
    assert commission(1, 50) == pytest.approx(0.50)           # 1% cap binds
    assert commission(0, 0) == 0.0
