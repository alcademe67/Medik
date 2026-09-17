"""Walk-forward / multi-crisis test of the frozen daily regime strategy.

Same FROZEN rules as backtest/daily_regime_bt.py (200-SMA regime, rising-slope +
3-month momentum entry, 15%-extension anti-chase, exit below the 200-SMA). No
parameter is fitted to data, so the ENTIRE ~20-year history is out-of-sample.

Method: run ONE continuous long-or-cash simulation per ETF over the full history
(no per-window resets), then measure the strategy vs buy-and-hold inside each
crash window and each sequential 5-year block by slicing the single continuous
equity curve. This is the honest test the ~5y run could not give: does the
strategy reduce drawdown through MULTIPLE independent crashes (2008, 2020, 2022)?

    python backtest/daily_regime_walkforward.py [--equity 10000]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.daily_regime_bt import (
    HALF_SPREAD_BPS, MAX_EXTENSION, MOM_LOOKBACK, SLIPPAGE_BPS, SLOPE_LOOKBACK,
    SMA_LONG, buy_hold, commission, simulate,
)

DATA = Path(r"C:\Users\Administrator\Medik\scratchpad\stockdata_long")
SYMBOLS = ["SPY", "QQQ", "IWM", "DIA", "XLF", "XLK"]

CRISES = [
    ("2008 GFC",      "2007-10-01", "2009-06-30"),
    ("2011 EU/debt",  "2011-05-01", "2011-12-31"),
    ("2015-16 China", "2015-07-01", "2016-06-30"),
    ("2018 Q4",       "2018-09-01", "2019-03-31"),
    ("2020 COVID",    "2020-02-01", "2020-06-30"),
    ("2022 bear",     "2022-01-01", "2022-12-31"),
]
BLOCKS = [
    ("2006-2011", "2006-01-01", "2010-12-31"),
    ("2011-2016", "2011-01-01", "2015-12-31"),
    ("2016-2021", "2016-01-01", "2020-12-31"),
    ("2021-2026", "2021-01-01", "2026-12-31"),
]


def load(sym):
    rows = json.loads((DATA / f"{sym}.json").read_text())
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df["sma200"] = df["close"].rolling(SMA_LONG).mean()
    df["sma200_prev"] = df["sma200"].shift(SLOPE_LOOKBACK)
    df["close_prev"] = df["close"].shift(MOM_LOOKBACK)
    return df


def window_perf(curve, s, e):
    """Return (ret%, maxDD%) of an equity curve inside [s,e]."""
    seg = curve[(curve.index >= pd.Timestamp(s)) & (curve.index <= pd.Timestamp(e))]
    if len(seg) < 2:
        return None
    ret = seg.iloc[-1] / seg.iloc[0] - 1
    peak = seg.cummax()
    dd = ((peak - seg) / peak).max()
    return ret * 100, dd * 100


def full_stats(curve):
    yrs = (curve.index[-1] - curve.index[0]).days / 365.25
    ret = curve.iloc[-1] / curve.iloc[0] - 1
    cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / yrs) - 1
    peak = curve.cummax(); dd = ((peak - curve) / peak).max()
    r = curve.pct_change().dropna()
    sharpe = (r.mean() * 252) / (r.std() * (252 ** 0.5)) if r.std() > 0 else 0.0
    return dict(ret=ret * 100, cagr=cagr * 100, maxdd=dd * 100, sharpe=sharpe,
                ret_dd=(ret * 100) / (dd * 100) if dd > 0 else float("inf"), yrs=yrs)


def main():
    equity = 10000.0
    a = sys.argv[1:]
    while a:
        x = a.pop(0)
        if x == "--equity":
            equity = float(a.pop(0))

    data = {s: load(s) for s in SYMBOLS}
    strat = {}; bh = {}; trades = {}; tim = {}
    for s, df in data.items():
        cur, tr, ti = simulate(df, equity)           # ONE continuous run
        strat[s] = cur; bh[s] = buy_hold(df, equity)
        trades[s] = tr; tim[s] = ti * 100
    # equal-weight portfolio = sum of sleeves (union of dates, ffill)
    def portfolio(d):
        idx = sorted(set().union(*[set(c.index) for c in d.values()]))
        p = pd.Series(0.0, index=idx)
        for c in d.values():
            p = p.add(c.reindex(idx).ffill().bfill(), fill_value=0)
        return p
    ps, pb = portfolio(strat), portfolio(bh)

    print(f"WALK-FORWARD / MULTI-CRISIS  equity/sleeve=${equity:,.0f}  (frozen rules; whole history is OOS)")
    print(f"data span {ps.index[0].date()}..{ps.index[-1].date()}\n")

    print("PER-ETF full history:  strategy vs buy&hold")
    print(f"{'ETF':<5}{'ret%':>9}{'cagr%':>7}{'maxDD%':>8}{'ret/DD':>7}{'shrp':>6}{'trds':>5}{'TIM%':>6}   | B&H ret% cagr% maxDD% r/DD shrp")
    for s in SYMBOLS:
        a1 = full_stats(strat[s]); b1 = full_stats(bh[s])
        print(f"{s:<5}{a1['ret']:>9.0f}{a1['cagr']:>7.1f}{a1['maxdd']:>8.1f}{a1['ret_dd']:>7.2f}"
              f"{a1['sharpe']:>6.2f}{trades[s]:>5}{tim[s]:>6.0f}   | {b1['ret']:>7.0f}{b1['cagr']:>6.1f}"
              f"{b1['maxdd']:>7.1f}{b1['ret_dd']:>5.2f}{b1['sharpe']:>5.2f}")

    pa, pbf = full_stats(ps), full_stats(pb)
    print(f"\nPORTFOLIO (6 EW) full {pa['yrs']:.1f}y:")
    print(f"  STRATEGY  ret {pa['ret']:.0f}%  CAGR {pa['cagr']:.1f}%  maxDD {pa['maxdd']:.1f}%  ret/DD {pa['ret_dd']:.2f}  Sharpe {pa['sharpe']:.2f}")
    print(f"  BUY&HOLD  ret {pbf['ret']:.0f}%  CAGR {pbf['cagr']:.1f}%  maxDD {pbf['maxdd']:.1f}%  ret/DD {pbf['ret_dd']:.2f}  Sharpe {pbf['sharpe']:.2f}")

    print("\nCRISIS WINDOWS (portfolio):   strategy   vs   buy&hold   -- the whole point")
    print(f"{'window':<16}{'strat ret%':>11}{'strat DD%':>10}   {'B&H ret%':>9}{'B&H DD%':>8}   {'DD saved':>9}")
    for name, s, e in CRISES:
        a1 = window_perf(ps, s, e); b1 = window_perf(pb, s, e)
        if not a1 or not b1:
            print(f"{name:<16}{'  (no data)':>11}"); continue
        saved = b1[1] - a1[1]
        print(f"{name:<16}{a1[0]:>11.1f}{a1[1]:>10.1f}   {b1[0]:>9.1f}{b1[1]:>8.1f}   {saved:>+8.1f}pp")

    print("\nSEQUENTIAL 5-YEAR BLOCKS (portfolio, continuous curve):")
    print(f"{'block':<12}{'strat ret%':>11}{'strat DD%':>10}   {'B&H ret%':>9}{'B&H DD%':>8}")
    for name, s, e in BLOCKS:
        a1 = window_perf(ps, s, e); b1 = window_perf(pb, s, e)
        if not a1 or not b1:
            continue
        print(f"{name:<12}{a1[0]:>11.1f}{a1[1]:>10.1f}   {b1[0]:>9.1f}{b1[1]:>8.1f}")


if __name__ == "__main__":
    main()
