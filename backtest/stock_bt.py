"""No-lookahead portfolio backtest for the stock decision engine.

    python backtest/stock_bt.py --data <dir> [--equity 500] [--bench SPY QQQ]

Reads <dir>/<SYMBOL>.json daily bars. Simulates the account as the engine would
trade it: up to MAX_POSITIONS concurrent longs, entries signalled on a day's
close and filled at the NEXT day's open, managed daily with a stop-first exit.

EXECUTION REALISM (same pessimism as backtest/medik_swing_bt.py):
  * entry fills at next open + half spread + slippage; exit crosses out the same
  * a gap through the stop fills at the OPEN, not the stop
  * commissions on both legs, the account's real schedule
    (clamp($0.005/share, min $1.00, max 1% of trade value)); fractional shares
    (enabled on the live account), so tiny positions pay the $1.00 minimum both
    ways -- the exact drag that must be earned through.

Portfolio: multiple signals on one day are ranked by 3-month momentum and the
best fill open slots within the 80% deployment cap. The per-sector cap in the
spec is NOT modelled here (no sector data) -- an omission that makes the test
MORE permissive, so a RED result here is if anything understated.

Indicators are precomputed once per symbol; the entry rule is the SAME
strategy.stock_engine.entry_from_row the live wrapper uses. Nothing here tunes
the frozen parameters.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.stock_engine import (
    MAX_DEPLOYED_PCT,
    MAX_POSITIONS,
    MIN_HISTORY,
    entry_from_row,
    precompute_features,
    size_stock,
    stock_exit,
)

SPREAD_BPS = 4.0
SLIPPAGE_BPS = 2.0
PER_SHARE, MIN_COMMISSION, MAX_PCT = 0.005, 1.00, 0.01
MOMENTUM_LOOKBACK = 63


def commission(qty: float, value: float) -> float:
    if qty <= 0 or value <= 0:
        return 0.0
    return min(max(PER_SHARE * qty, MIN_COMMISSION), MAX_PCT * value)


def _friction(price: float) -> float:
    return price * ((SPREAD_BPS / 2 + SLIPPAGE_BPS) / 10_000)


def load(data_dir: Path) -> dict[str, pd.DataFrame]:
    frames = {}
    for path in sorted(data_dir.glob("*.json")):
        rows = json.loads(path.read_text())
        df = pd.DataFrame(rows)
        if df.empty or not {"open", "high", "low", "close", "volume"} <= set(df.columns):
            continue
        df["date"] = pd.to_datetime(df["date"])
        frames[path.stem] = df.set_index("date").sort_index()
    return frames


def prep(frames: dict[str, pd.DataFrame]) -> dict:
    """Precompute features once per symbol; return {sym: (feat_df, {date:i})}."""
    feats = {}
    for sym, df in frames.items():
        f = precompute_features(df).reset_index()      # 'date' column + RangeIndex
        pos = {d: i for i, d in enumerate(f["date"])}
        feats[sym] = (f, pos)
    return feats


def run(feats: dict, equity: float, universe: list[str], start=None, end=None) -> dict:
    all_dates = sorted(set().union(*[set(feats[s][1]) for s in feats]))
    if start is not None:
        all_dates = [d for d in all_dates if d >= start]
    if end is not None:
        all_dates = [d for d in all_dates if d <= end]

    cash = equity
    positions: dict[str, dict] = {}
    pending: list[dict] = []
    trades: list[dict] = []
    curve = []
    peak = equity
    max_dd = 0.0

    def price_at(sym, day, col="close"):
        f, pos = feats[sym]
        i = pos.get(day)
        return None if i is None else float(f.iloc[i][col])

    def mark_equity(day):
        val = cash
        for sym, p in positions.items():
            px = price_at(sym, day) or p["entry"]
            val += p["qty"] * px
        return val

    for k, day in enumerate(all_dates):
        deployed = sum(p["qty"] * p["entry"] for p in positions.values())
        # 1) fill pending entries at today's open
        for sig in pending:
            sym = sig["symbol"]
            f, pos = feats[sym]
            i = pos.get(day)
            if sym in positions or i is None:
                continue
            o = float(f.iloc[i]["open"]); fill = o + _friction(o)
            eq = mark_equity(day)
            qty = size_stock(eq, cash, deployed, fill, sig["stop"])
            if qty <= 0:
                continue
            cost = qty * fill; comm = commission(qty, cost)
            if cost + comm > cash:
                continue
            cash -= cost + comm; deployed += cost
            positions[sym] = {"qty": qty, "entry": fill, "stop": sig["stop"],
                              "highest_close": fill, "sessions": 0, "below50": 0,
                              "entry_day": day, "entry_cost": comm}
        pending = []

        # 2) manage open positions on today's bar
        for sym in list(positions):
            f, pos = feats[sym]
            i = pos.get(day)
            if i is None:
                continue
            row = f.iloc[i]; p = positions[sym]
            p["sessions"] += 1
            sma50 = float(row["sma50"]); atr_now = float(row["atr"])
            p["below50"] = p["below50"] + 1 if float(row["close"]) < sma50 else 0
            p["highest_close"] = max(p["highest_close"], float(row["close"]))
            done, raw_exit, why = stock_exit(
                float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]),
                p["stop"], p["highest_close"], atr_now, p["below50"], p["sessions"])
            if done:
                exit_px = raw_exit - _friction(raw_exit)
                proceeds = p["qty"] * exit_px; comm = commission(p["qty"], proceeds)
                cash += proceeds - comm
                pnl = p["qty"] * (exit_px - p["entry"]) - p["entry_cost"] - comm
                trades.append({"symbol": sym, "entry_day": str(p["entry_day"].date()),
                               "exit_day": str(day.date()), "sessions": p["sessions"],
                               "why": why, "pnl": pnl, "commission": p["entry_cost"] + comm})
                del positions[sym]

        # 3) mark equity / drawdown
        val = mark_equity(day)
        curve.append(val)
        peak = max(peak, val)
        if peak > 0:
            max_dd = max(max_dd, (peak - val) / peak)

        # 4) look for new entries on today's close (fill tomorrow)
        slots = MAX_POSITIONS - len(positions)
        deployed = sum(p["qty"] * p["entry"] for p in positions.values())
        if slots > 0 and deployed < MAX_DEPLOYED_PCT * val and k < len(all_dates) - 1:
            cands = []
            for sym in universe:
                if sym not in feats or sym in positions:
                    continue
                f, pos = feats[sym]
                i = pos.get(day)
                if i is None or i < MIN_HISTORY:
                    continue
                sig = entry_from_row(f, i)
                if sig.passed:
                    mom = float(f.iloc[i]["close"] / f.iloc[i - MOMENTUM_LOOKBACK]["close"] - 1) \
                        if i > MOMENTUM_LOOKBACK else 0.0
                    cands.append((mom, sym, sig.stop))
            cands.sort(reverse=True)
            pending = [{"symbol": s, "stop": st} for _, s, st in cands[:slots]]

    final = curve[-1] if curve else equity
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gw = sum(t["pnl"] for t in wins); gl = -sum(t["pnl"] for t in losses)
    gross_before_comm = sum(t["pnl"] + t["commission"] for t in trades)
    tot_comm = sum(t["commission"] for t in trades)
    return {
        "start": str(all_dates[0].date()) if all_dates else "",
        "end": str(all_dates[-1].date()) if all_dates else "",
        "final": final, "net": final - equity, "net_pct": (final / equity - 1) * 100,
        "n": len(trades), "win_rate": len(wins) / len(trades) if trades else 0.0,
        "pf": gw / gl if gl > 0 else float("inf"),
        "expectancy": (final - equity) / len(trades) if trades else 0.0,
        "commission": tot_comm,
        "comm_pct_of_gross": (tot_comm / gross_before_comm * 100) if gross_before_comm > 0 else float("inf"),
        "max_dd": max_dd * 100,
        "exits": _exit_counts(trades),
    }


def _exit_counts(trades):
    d = {}
    for t in trades:
        d[t["why"]] = d.get(t["why"], 0) + 1
    return d


def buy_hold(feats, symbol, equity, start=None, end=None):
    if symbol not in feats:
        return 0.0, 0.0
    f = feats[symbol][0]
    m = f["date"]
    idx = f[(m >= (start or m.min())) & (m <= (end or m.max()))]
    if len(idx) < 2:
        return 0.0, 0.0
    first = float(idx.iloc[0]["open"]); last = float(idx.iloc[-1]["close"])
    qty = (equity - 1.0) / first
    series = idx["close"] * qty
    peak = series.cummax(); dd = ((peak - series) / peak).max() * 100
    return qty * last - 1.0 - equity, float(dd)


def report(label, r, equity):
    print(f"\n===== {label} =====")
    print(f"window {r['start']} -> {r['end']}   trades {r['n']}   win {r['win_rate']:.1%}   exits {r['exits']}")
    print(f"NET ${r['net']:+.2f} ({r['net_pct']:+.1f}%)   PF {r['pf']:.2f}   "
          f"expectancy ${r['expectancy']:+.3f}/trade   maxDD {r['max_dd']:.1f}%")
    print(f"commissions ${r['commission']:.2f} = {r['comm_pct_of_gross']:.0f}% of gross profit")


def main():
    data_dir = None; equity = 500.0; bench = ["SPY", "QQQ"]
    args = sys.argv[1:]
    while args:
        a = args.pop(0)
        if a == "--data": data_dir = Path(args.pop(0))
        elif a == "--equity": equity = float(args.pop(0))
        elif a == "--bench":
            bench = []
            while args and not args[0].startswith("--"): bench.append(args.pop(0))
        else: print(f"unknown option {a!r}"); return
    if data_dir is None:
        print("--data <dir> required"); return

    frames = load(data_dir)
    if not frames:
        print("no data"); return
    feats = prep(frames)
    universe = [s for s in feats if s not in bench]
    print(f"loaded {len(feats)} symbols; universe {len(universe)}, benchmarks {bench}")

    all_dates = sorted(set().union(*[set(feats[s][1]) for s in feats]))
    split = all_dates[int(len(all_dates) * 0.6)]

    full = run(feats, equity, universe)
    report(f"FULL PERIOD @ ${equity:,.0f}", full, equity)
    ins = run(feats, equity, universe, end=split)
    report("IN-SAMPLE (first 60%)", ins, equity)
    oos = run(feats, equity, universe, start=split)
    report("OUT-OF-SAMPLE (final 40%)", oos, equity)

    print("\n===== benchmarks (same OOS window, buy & hold, $1 commissions) =====")
    bh = {}
    for b in bench:
        net, dd = buy_hold(feats, b, equity, start=split)
        bh[b] = net
        print(f"{b} buy & hold: ${net:+.2f} ({net/equity*100:+.1f}%)  maxDD {dd:.1f}%")

    print("\n===== ACCEPTANCE GATES (OOS) =====")
    gates = {
        "1. OOS profit factor >= 1.3": oos["pf"] >= 1.3,
        "2. OOS net expectancy > 0": oos["expectancy"] > 0,
        "3. OOS max drawdown <= 25%": oos["max_dd"] <= 25.0,
        "4. beats SPY & QQQ buy&hold (net)": all(oos["net"] > bh.get(b, 0) for b in bench),
        "5. >=30 OOS trades AND commissions <20% of gross": oos["n"] >= 30 and oos["comm_pct_of_gross"] < 20.0,
        "6. robust: full-period PF>=1.3 & net>0 too": full["pf"] >= 1.3 and full["net"] > 0,
    }
    for name, ok in gates.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    ok_all = all(gates.values())
    print(f"\nVERDICT: {'GREEN — all gates pass' if ok_all else 'RED — one or more gates failed'}")
    print("Per spec: RED means DO NOT enable live stock trading; do not retune against OOS.")
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
