"""Low-frequency monthly cross-sectional momentum — research test only.

    python backtest/monthly_momentum_bt.py --data <dir> [--equity 500] [--top 3]

Each month-end, rank the universe by 6-month total return; hold the top N names
that ALSO have positive 6-month absolute momentum (else that slot stays cash).
Minimal turnover: only sells names that fall out of the top set and buys new
entrants -- continuing holds are left alone, so trades (and commissions) are
few. Execution is no-lookahead (decision on the month-end close, fill at the
next session's open + friction), with the account's real commission schedule.

This is deliberately the OPPOSITE of the daily engine: it trades ~a few times a
year, so if active stock trading is viable at this size at all, this is where it
should show. Frozen rules; same acceptance gates; no OOS tuning.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.stock_bt import load, buy_hold, prep, _friction, commission

MOM_LOOKBACK = 126        # ~6 months of trading days
MAX_DEPLOYED = 0.95       # keep a small cash buffer


def month_end_dates(dates):
    """Last trading day within each calendar month."""
    s = pd.Series(dates, index=pd.DatetimeIndex(dates))
    return list(s.groupby([s.index.year, s.index.month]).last())


def run(feats, equity, top_n, start=None, end=None):
    all_dates = sorted(set().union(*[set(feats[s][1]) for s in feats]))
    if start is not None:
        all_dates = [d for d in all_dates if d >= start]
    if end is not None:
        all_dates = [d for d in all_dates if d <= end]
    date_index = {d: i for i, d in enumerate(all_dates)}
    rebal = set(month_end_dates(all_dates))
    universe = list(feats.keys())

    cash = equity
    positions: dict[str, float] = {}     # sym -> qty
    pending = None                        # (sells, buys) to execute at next open
    trades = []
    curve = []
    peak = equity
    max_dd = 0.0

    def px(sym, day, col="close"):
        f, pos = feats[sym]
        i = pos.get(day)
        return None if i is None else float(f.iloc[i][col])

    def mark(day):
        v = cash
        for s, q in positions.items():
            v += q * (px(s, day) or 0)
        return v

    for day in all_dates:
        # execute pending rebalance at today's open
        if pending is not None:
            sells, buys = pending
            for s in sells:
                if s in positions:
                    o = px(s, day, "open")
                    if o is None:
                        continue
                    fill = o - _friction(o); proceeds = positions[s] * fill
                    comm = commission(positions[s], proceeds)
                    cash += proceeds - comm
                    trades.append({"pnl_leg": "sell", "commission": comm})
                    del positions[s]
            if buys:
                eq = mark(day)
                per = min(eq / top_n, (MAX_DEPLOYED * eq - sum(positions[s] * (px(s, day) or 0) for s in positions)) / max(1, len(buys)))
                for s in buys:
                    o = px(s, day, "open")
                    if o is None or per <= 0:
                        continue
                    fill = o + _friction(o); budget = min(per, cash)
                    qty = budget / fill
                    if qty <= 0:
                        continue
                    cost = qty * fill; comm = commission(qty, cost)
                    if cost + comm > cash:
                        continue
                    cash -= cost + comm; positions[s] = positions.get(s, 0) + qty
                    trades.append({"pnl_leg": "buy", "commission": comm})
            pending = None

        v = mark(day)
        curve.append(v)
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)

        # decide next rebalance on a month-end close
        if day in rebal and date_index[day] < len(all_dates) - 1:
            ranked = []
            for s in universe:
                f, pos = feats[s]
                i = pos.get(day)
                if i is None or i < MOM_LOOKBACK:
                    continue
                mom = float(f.iloc[i]["close"] / f.iloc[i - MOM_LOOKBACK]["close"] - 1)
                if mom > 0:                      # absolute-momentum filter
                    ranked.append((mom, s))
            ranked.sort(reverse=True)
            target = {s for _, s in ranked[:top_n]}
            sells = [s for s in positions if s not in target]
            buys = [s for s in target if s not in positions]
            if sells or buys:
                pending = (sells, buys)

    final = curve[-1] if curve else equity
    n_round = len(trades)
    tot_comm = sum(t["commission"] for t in trades)
    return {"start": str(all_dates[0].date()) if all_dates else "",
            "end": str(all_dates[-1].date()) if all_dates else "",
            "final": final, "net": final - equity, "net_pct": (final / equity - 1) * 100,
            "legs": n_round, "commission": tot_comm, "max_dd": max_dd * 100}


def main():
    data_dir = None; equity = 500.0; top_n = 3; bench = ["SPY", "QQQ"]
    args = sys.argv[1:]
    while args:
        a = args.pop(0)
        if a == "--data": data_dir = Path(args.pop(0))
        elif a == "--equity": equity = float(args.pop(0))
        elif a == "--top": top_n = int(args.pop(0))
        else: print(f"unknown {a!r}"); return
    if data_dir is None:
        print("--data required"); return
    frames = load(data_dir); feats = prep(frames)
    # benchmarks excluded from the tradable momentum universe
    feats_universe = {s: v for s, v in feats.items() if s not in bench}
    all_dates = sorted(set().union(*[set(feats[s][1]) for s in feats]))
    split = all_dates[int(len(all_dates) * 0.6)]

    def show(label, r):
        print(f"\n{label}: {r['start']}->{r['end']}  net ${r['net']:+.2f} ({r['net_pct']:+.1f}%)  "
              f"trade-legs {r['legs']}  commissions ${r['commission']:.2f}  maxDD {r['max_dd']:.1f}%")

    full = run(feats_universe, equity, top_n)
    ins = run(feats_universe, equity, top_n, end=split)
    oos = run(feats_universe, equity, top_n, start=split)
    print(f"MONTHLY MOMENTUM (top {top_n}, 6mo lookback, monthly, @ ${equity:,.0f})")
    show("FULL ", full); show("IN-SAMPLE ", ins); show("OUT-OF-SAMPLE", oos)

    print("\nbenchmarks (OOS, buy & hold):")
    bh = {}
    for b in bench:
        net, dd = buy_hold(feats, b, equity, start=split); bh[b] = net
        print(f"  {b}: {net/equity*100:+.1f}%  maxDD {dd:.1f}%")

    print("\nACCEPTANCE GATES (OOS):")
    gates = {
        "OOS net expectancy > 0": oos["net"] > 0,
        "OOS max drawdown <= 25%": oos["max_dd"] <= 25.0,
        "beats SPY & QQQ buy&hold (net)": all(oos["net"] > bh[b] for b in bench),
        "robust: full-period net > 0 too": full["net"] > 0,
    }
    for k, ok in gates.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {k}")
    ok_all = all(gates.values())
    print(f"\nVERDICT: {'GREEN' if ok_all else 'RED'}")
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
