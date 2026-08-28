"""The swing strategy module computes what the backtest measured.

Kept even though the strategy tested RED (see CLAUDE.md, 2026-08-27): the
module documents the owner's style as specified, and these tests pin the
mechanics so the recorded verdict stays reproducible.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.medik_swing import (
    MAX_HOLD_SESSIONS,
    STOP_PCT,
    SwingSignal,
    evaluate_swing,
    size_swing,
    swing_exit,
)


def _uptrend_with_reclaim(n: int = 260) -> pd.DataFrame:
    """Synthetic rising series ending in a pullback-to-EMA reclaim."""
    closes = [100 + 0.3 * i for i in range(n)]
    # pullback: five soft days dipping to the 8-EMA zone, then a reclaim
    closes[-6] = closes[-7] - 1.2
    closes[-5] = closes[-6] - 1.0
    closes[-4] = closes[-5] - 0.8
    closes[-3] = closes[-4] - 0.4
    closes[-2] = closes[-3] + 0.2
    closes[-1] = closes[-2] + 1.6          # reclaim day
    rows = []
    for i, c in enumerate(closes):
        rows.append({"open": c - 0.2, "high": c + 0.9, "low": c - 0.9,
                     "close": c, "volume": 1_000_000})
    # the reclaim day makes a higher low than the pullback low
    rows[-1]["low"] = rows[-1]["close"] - 0.3
    # a prior swing high far enough above to pay 1:1 against the -2.5% stop
    rows[-8]["high"] = rows[-1]["close"] * 1.06
    return pd.DataFrame(rows)


def test_reclaim_produces_signal_with_fixed_stop():
    sig = evaluate_swing(_uptrend_with_reclaim())
    assert isinstance(sig, SwingSignal)
    assert sig.passed, sig.reason
    assert abs(sig.stop - sig.entry * (1 - STOP_PCT)) < 1e-9
    assert sig.target > sig.entry
    assert sig.reward_risk >= 1.0


def test_downtrend_is_refused():
    closes = [200 - 0.3 * i for i in range(260)]
    df = pd.DataFrame({"open": closes, "high": [c + 1 for c in closes],
                       "low": [c - 1 for c in closes], "close": closes,
                       "volume": [1_000_000] * 260})
    assert not evaluate_swing(df).passed


def test_sizing_uses_deploy_fraction_and_guards_dust():
    qty = size_swing(286.15, 72.50)
    assert 3.0 < qty < 4.0
    assert size_swing(20.0, 72.50) == 0.0       # dust guard
    assert size_swing(286.15, 0.0) == 0.0


def test_exit_order_is_pessimistic():
    # gap through the stop fills at the open
    done, price, why = swing_exit(95.0, 99.0, 94.0, 98.0,
                                  stop=97.0, target=110.0, sessions_held=1)
    assert done and price == 95.0 and why == "gap through stop"
    # stop before target when one bar spans both
    done, price, why = swing_exit(100.0, 111.0, 96.0, 105.0,
                                  stop=97.0, target=110.0, sessions_held=1)
    assert done and price == 97.0 and why == "stop"
    # target fills at the target, no improvement
    done, price, why = swing_exit(100.0, 111.0, 99.0, 108.0,
                                  stop=97.0, target=110.0, sessions_held=1)
    assert done and price == 110.0 and why == "target"
    # time exit at the close of the last allowed session
    done, price, why = swing_exit(100.0, 101.0, 99.5, 100.4,
                                  stop=97.0, target=110.0,
                                  sessions_held=MAX_HOLD_SESSIONS)
    assert done and price == 100.4 and why == "time exit"
    # otherwise keep holding
    done, _, _ = swing_exit(100.0, 101.0, 99.5, 100.4,
                            stop=97.0, target=110.0, sessions_held=1)
    assert not done
