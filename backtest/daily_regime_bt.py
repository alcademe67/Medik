"""Daily trend/regime ETF strategy — FROZEN research backtest.

OBJECTIVE (owner, 2026-09-17): capture sustained upside momentum and move to
cash when the trend materially deteriorates — NOT to maximize trade frequency.
Long-or-cash only (TFSA, no shorting). Research only; NOT wired to the live bot.

FROZEN RULES (defined before looking at OOS; standard trend-following, not fit
to this data):
  State per ETF is HOLD (fully invested in that sleeve) or CASH.
  * ENTER (cash -> hold) when ALL hold:
      close > SMA200                      (in an uptrend)
      SMA200 rising: SMA200 > SMA200[-20] (the trend itself is turning up)
      3-month momentum > 0: close > close[-63]
      NOT extended: close <= 1.15 * SMA200 (objective #7 — never chase a blowoff
                                            on a FRESH entry)
  * EXIT (hold -> cash) when: close < SMA200. One decisive regime break.
    (Extension never forces an exit — we only refuse to CHASE on entry, we do
    not sell a working uptrend for being strong.)

EXECUTION REALISM: decide on the daily close, fill at the NEXT day's open with
slippage; real commission clamp($0.005/sh, min $1, max 1% of value); fractional
shares (live account). Daily cadence => few round-trips => low commission drag.

PORTFOLIO: six independent equal-weight sleeves (equity/6 each); a sleeve in
cash earns 0 (conservative). Compared against buy-and-hold of the same ETF.

IS/OOS: first 60% of the shared timeline is in-sample, final 40% out-of-sample.
ALL parameters (200, 20, 63, 1.15) are frozen before the OOS window is read.

ACCEPTANCE (frozen — the objective is drawdown-aware capture, not raw return):
a genuine edge = OOS net return positive after costs AND OOS return-per-maxDD
beats buy-and-hold AND max drawdown materially lower than B&H (<= ~65% of it),
holding across a majority of the six ETFs. Merely cutting drawdown by giving up
most of the return is reported honestly, not called an edge.

    python backtest/daily_regime_bt.py [--equity 10000]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATA = Path(r"C:\Users\Administrator\Medik\scratchpad\stockdata")
SYMBOLS = ["SPY", "QQQ", "IWM", "DIA", "XLF", "XLK"]

SMA_LONG = 200
SLOPE_LOOKBACK = 20
MOM_LOOKBACK = 63
MAX_EXTENSION = 1.15
PER_SHARE, MIN_COMM, MAX_PCT = 0.005, 1.00, 0.01
SLIPPAGE_BPS = 2.0
HALF_SPREAD_BPS = 1.0


def commission(sh, val):
    return 0.0 if sh <= 0 or val <= 0 else min(max(PER_SHARE * sh, MIN_COMM), MAX_PCT * val)


def load(sym):
    rows = json.loads((DATA / f"{sym}.json").read_text())
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df["sma200"] = df["close"].rolling(SMA_LONG).mean()
    df["sma200_prev"] = df["sma200"].shift(SLOPE_LOOKBACK)
    df["close_prev"] = df["close"].shift(MOM_LOOKBACK)
    return df


def simulate(df, equity, start=None, end=None):
    """Long-or-cash sleeve. Returns (equity_series, trades, time_in_market_frac)."""
    idx = df.index
    if start is not None:
        idx = idx[idx >= start]
    if end is not None:
        idx = idx[idx <= end]
    idx = list(idx)
    cash = equity
    shares = 0.0
    state = "CASH"
    trades = 0
    days_in = 0
    curve = []
    for k, d in enumerate(idx):
        row = df.loc[d]
        # mark equity at today's close
        eq = cash + shares * row["close"]
        curve.append((d, eq))
        if shares > 0:
            days_in += 1
        # decide on today's close, act at NEXT open
        if k + 1 >= len(idx):
            continue
        nd = idx[k + 1]
        nopen = df.loc[nd, "open"]
        c, sma, sma_prev, cprev = row["close"], row["sma200"], row["sma200_prev"], row["close_prev"]
        if pd.isna(sma) or pd.isna(sma_prev) or pd.isna(cprev):
            continue
        if state == "CASH":
            enter = (c > sma) and (sma > sma_prev) and (c > cprev) and (c <= MAX_EXTENSION * sma)
            if enter:
                fill = nopen * (1 + (SLIPPAGE_BPS + HALF_SPREAD_BPS) / 10_000.0)
                qty = cash / fill
                comm = commission(qty, qty * fill)
                # solve so cost+comm <= cash (comm is <=1% so shave)
                qty = (cash - comm) / fill
                if qty <= 0:
                    continue
                comm = commission(qty, qty * fill)
                cash -= qty * fill + comm
                shares = qty
                state = "HOLD"
                trades += 1
        else:  # HOLD
            if c < sma:
                fill = nopen * (1 - (SLIPPAGE_BPS + HALF_SPREAD_BPS) / 10_000.0)
                proceeds = shares * fill
                comm = commission(shares, proceeds)
                cash += proceeds - comm
                shares = 0.0
                state = "CASH"
                trades += 1
    s = pd.Series({d: e for d, e in curve})
    return s, trades, (days_in / len(idx) if idx else 0.0)


def buy_hold(df, equity, start=None, end=None):
    idx = df.index
    if start is not None:
        idx = idx[idx >= start]
    if end is not None:
        idx = idx[idx <= end]
    idx = list(idx)
    if len(idx) < 2:
        return pd.Series(dtype=float)
    o0 = df.loc[idx[0], "open"]
    fill = o0 * (1 + (SLIPPAGE_BPS + HALF_SPREAD_BPS) / 10_000.0)
    comm = commission((equity - MIN_COMM) / fill, equity)
    qty = (equity - comm) / fill
    return pd.Series({d: qty * df.loc[d, "close"] for d in idx})


def stats(curve, years, trades=0, tim=None):
    if curve is None or len(curve) < 2:
        return None
    start_v, end_v = curve.iloc[0], curve.iloc[-1]
    ret = end_v / start_v - 1
    cagr = (end_v / start_v) ** (1 / years) - 1 if years > 0 and start_v > 0 else 0.0
    peak = curve.cummax()
    dd = ((peak - curve) / peak).max()
    rets = curve.pct_change().dropna()
    vol = rets.std() * (252 ** 0.5) if len(rets) > 1 else 0.0
    sharpe = (rets.mean() * 252) / (rets.std() * (252 ** 0.5)) if rets.std() > 0 else 0.0
    return dict(ret=ret * 100, cagr=cagr * 100, maxdd=dd * 100, vol=vol * 100,
                sharpe=sharpe, trades=trades, tim=(tim * 100 if tim is not None else None),
                ret_per_dd=(ret * 100) / (dd * 100) if dd > 0 else float("inf"),
                final=end_v)


def yrs(idx):
    return (idx[-1] - idx[0]).days / 365.25


def main():
    equity = 10000.0
    a = sys.argv[1:]
    while a:
        x = a.pop(0)
        if x == "--equity":
            equity = float(a.pop(0))
    data = {s: load(s) for s in SYMBOLS}
    common = sorted(set.intersection(*[set(df.index) for df in data.values()]))
    split = common[int(len(common) * 0.6)]
    print(f"DAILY REGIME STRATEGY  equity/sleeve=${equity:,.0f}")
    print(f"data {common[0].date()} .. {common[-1].date()}  ({len(common)} days)  IS/OOS split @ {split.date()}\n")

    for label, s0, s1 in [("IN-SAMPLE", common[0], split), ("OUT-OF-SAMPLE", split, common[-1])]:
        yy = (s1 - s0).days / 365.25
        print(f"===== {label}  {s0.date()}..{s1.date()}  ({yy:.2f}y) =====")
        print(f"{'ETF':<5}{'  strategy: ret%  CAGR%  maxDD%  ret/DD  vol%  Shrp  trds  TIM%':<64}")
        agg_strat = None; agg_bh = None
        port_rows = []
        for s in SYMBOLS:
            df = data[s]
            cur, tr, tim = simulate(df, equity, s0, s1)
            bh = buy_hold(df, equity, s0, s1)
            st = stats(cur, yy, tr, tim); bh_st = stats(bh, yy)
            print(f"{s:<5} STRAT ret{st['ret']:>7.1f}  cagr{st['cagr']:>6.1f}  dd{st['maxdd']:>6.1f}"
                  f"  r/dd{st['ret_per_dd']:>6.2f}  vol{st['vol']:>5.1f}  sh{st['sharpe']:>5.2f}"
                  f"  t{st['trades']:>3}  tim{st['tim']:>5.0f}")
            print(f"{'':<5} B&H   ret{bh_st['ret']:>7.1f}  cagr{bh_st['cagr']:>6.1f}  dd{bh_st['maxdd']:>6.1f}"
                  f"  r/dd{bh_st['ret_per_dd']:>6.2f}  vol{bh_st['vol']:>5.1f}  sh{bh_st['sharpe']:>5.2f}")
            agg_strat = cur if agg_strat is None else agg_strat.add(cur, fill_value=0)
            agg_bh = bh if agg_bh is None else agg_bh.add(bh, fill_value=0)
            port_rows.append((s, st, bh_st))
        # equal-weight portfolio (sum of sleeves)
        ps = stats(agg_strat, yy); pb = stats(agg_bh, yy)
        wins_dd = sum(1 for _, st, bh in port_rows if st["maxdd"] < bh["maxdd"])
        wins_rdd = sum(1 for _, st, bh in port_rows if st["ret_per_dd"] > bh["ret_per_dd"])
        print(f"\n  PORTFOLIO(6 EW)  STRAT ret{ps['ret']:>6.1f}%  cagr{ps['cagr']:>5.1f}%  maxDD{ps['maxdd']:>5.1f}%  ret/DD{ps['ret_per_dd']:>5.2f}  sharpe{ps['sharpe']:>5.2f}")
        print(f"                   B&H   ret{pb['ret']:>6.1f}%  cagr{pb['cagr']:>5.1f}%  maxDD{pb['maxdd']:>5.1f}%  ret/DD{pb['ret_per_dd']:>5.2f}  sharpe{pb['sharpe']:>5.2f}")
        print(f"  per-ETF: strategy cut drawdown on {wins_dd}/6 ; beat B&H return-per-drawdown on {wins_rdd}/6\n")


if __name__ == "__main__":
    main()
