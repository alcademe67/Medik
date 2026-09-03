"""Backtest of MEDIK SWING — the owner's 1-5 day single-position rotation.

    python backtest/medik_swing_bt.py --data <dir> [--equity 286.15]

Reads <dir>/<SYMBOL>.json daily bars (written by the fetch script) and
simulates the account the way the owner described trading it: flat or in
exactly ONE ETF, entries signalled on a day's close and filled at the NEXT
day's open, exits by target / -2.5% stop / 5-session time-out.

EXECUTION REALISM (same pessimism as backtest/medik_etf_bt.py):
  * entry fills at next open plus half the spread plus slippage
  * a gap through the stop fills at the OPEN, not the stop
  * stop assumed BEFORE target when one bar spans both
  * the limit target fills at the target with no improvement, minus half
    spread (crossing out) — commissions on both fills, the account's real
    schedule: clamp($0.005/share, min $1.00, max 1% of trade value)
  * fractional shares allowed (enabled on the account)

The strategy logic itself is imported from strategy.medik_swing — the
thing measured is the thing that would trade.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.medik_etf_v2 import spread_bps
from strategy.medik_swing import (
    MAX_HOLD_SESSIONS,
    SWING_UNIVERSE,
    evaluate_swing,
    size_swing,
    swing_exit,
)

SLIPPAGE_BPS = 2.0
MIN_HISTORY = 210          # evaluate_swing needs the 200-SMA


def commission(qty: float, value: float) -> float:
    if qty <= 0 or value <= 0:
        return 0.0
    return min(max(0.005 * qty, 1.00), 0.01 * value)


def _friction(symbol: str, price: float) -> float:
    """One-way price penalty: half the spread plus slippage."""
    return price * ((spread_bps(symbol) / 2 + SLIPPAGE_BPS) / 10_000)


def load(data_dir: Path, symbols=None) -> dict[str, pd.DataFrame]:
    frames = {}
    for symbol in (symbols or SWING_UNIVERSE):
        path = data_dir / f"{symbol}.json"
        if not path.exists():
            continue
        rows = json.loads(path.read_text())
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        frames[symbol] = df.set_index("date")
    return frames


def run(frames: dict[str, pd.DataFrame], equity: float,
        start=None, end=None) -> dict:
    dates = sorted(set().union(*[set(df.index) for df in frames.values()]))
    if start is not None:
        dates = [d for d in dates if d >= start]
    if end is not None:
        dates = [d for d in dates if d <= end]

    cash = equity
    position = None            # dict(symbol qty fill stop target sessions)
    pending = None             # signal chosen on yesterday's close
    trades = []
    equity_curve = []
    peak = equity
    max_dd = 0.0

    for i, day in enumerate(dates):
        # ---- fill a pending entry at today's open
        if pending is not None and position is None:
            symbol = pending["symbol"]
            df = frames[symbol]
            if day in df.index:
                bar = df.loc[day]
                fill = float(bar["open"]) + _friction(symbol, float(bar["open"]))
                qty = size_swing(cash, fill)
                if qty > 0:
                    entry_cost = commission(qty, qty * fill)
                    cash -= qty * fill + entry_cost
                    position = {
                        "symbol": symbol, "qty": qty, "fill": fill,
                        "stop": fill * (1 - 0.025),
                        "target": pending["target"],
                        "sessions": 0, "entry_day": day,
                        "entry_cost": entry_cost,
                    }
            pending = None

        # ---- manage the open position on today's bar
        if position is not None:
            symbol = position["symbol"]
            df = frames[symbol]
            if day in df.index:
                bar = df.loc[day]
                position["sessions"] += 1
                done, raw_exit, why = swing_exit(
                    float(bar["open"]), float(bar["high"]), float(bar["low"]),
                    float(bar["close"]), position["stop"], position["target"],
                    position["sessions"])
                if done:
                    exit_price = raw_exit - _friction(symbol, raw_exit)
                    qty = position["qty"]
                    exit_cost = commission(qty, qty * exit_price)
                    cash += qty * exit_price - exit_cost
                    pnl = (qty * (exit_price - position["fill"])
                           - position["entry_cost"] - exit_cost)
                    trades.append({
                        "symbol": symbol, "entry_day": str(position["entry_day"].date()),
                        "exit_day": str(day.date()), "sessions": position["sessions"],
                        "fill": position["fill"], "exit": exit_price, "why": why,
                        "pnl": pnl,
                        "commission": position["entry_cost"] + exit_cost,
                    })
                    position = None

        # ---- mark equity, track drawdown
        value = cash
        if position is not None:
            df = frames[position["symbol"]]
            if day in df.index:
                value += position["qty"] * float(df.loc[day]["close"])
            else:
                value += position["qty"] * position["fill"]
        equity_curve.append((day, value))
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)

        # ---- if flat, look for tomorrow's entry on today's closes
        if position is None and pending is None and i < len(dates) - 1:
            best = None
            for symbol, df in frames.items():
                hist = df.loc[:day]
                if len(hist) < MIN_HISTORY or day not in df.index:
                    continue
                sig = evaluate_swing(hist.reset_index(drop=True))
                if sig.passed and (best is None or sig.reward_risk > best["rr"]):
                    best = {"symbol": symbol, "rr": sig.reward_risk,
                            "target": sig.target}
            if best is not None:
                pending = best

    final = equity_curve[-1][1] if equity_curve else equity
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    return {
        "start": str(dates[0].date()) if dates else "",
        "end": str(dates[-1].date()) if dates else "",
        "final": final, "net": final - equity, "net_pct": (final / equity - 1) * 100,
        "trades": trades, "n": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": gross_loss / len(losses) if losses else 0.0,
        "pf": gross_win / gross_loss if gross_loss > 0 else float("inf"),
        "commission": sum(t["commission"] for t in trades),
        "max_dd": max_dd * 100,
        "avg_sessions": (sum(t["sessions"] for t in trades) / len(trades))
                        if trades else 0.0,
    }


def buy_hold(frames: dict[str, pd.DataFrame], symbol: str, equity: float,
             start=None, end=None) -> float:
    df = frames[symbol]
    idx = df.index
    if start is not None:
        idx = idx[idx >= start]
    if end is not None:
        idx = idx[idx <= end]
    if len(idx) < 2:
        return 0.0
    first, last = float(df.loc[idx[0]]["open"]), float(df.loc[idx[-1]]["close"])
    qty = (equity - 1.0) / first          # $1 commission to get in
    return qty * last - 1.0 - equity      # ... and $1 to get out; net dollars


def report(label: str, r: dict, equity: float) -> None:
    print(f"\n===== {label} =====")
    print(f"window {r['start']} -> {r['end']}")
    print(f"trades {r['n']}   win rate {r['win_rate']:.1%}   "
          f"avg hold {r['avg_sessions']:.1f} sessions")
    print(f"avg win ${r['avg_win']:+.2f}   avg loss ${-r['avg_loss']:+.2f}   "
          f"profit factor {r['pf']:.2f}")
    print(f"commissions ${r['commission']:.2f}")
    print(f"NET ${r['net']:+.2f}  ({r['net_pct']:+.1f}% of ${equity:,.2f})   "
          f"max drawdown {r['max_dd']:.1f}%")
    exits = {}
    for t in r["trades"]:
        exits[t["why"]] = exits.get(t["why"], 0) + 1
    print("exits: " + ", ".join(f"{k} {v}" for k, v in sorted(exits.items())))


def main() -> None:
    data_dir = None
    equity = 286.15
    symbols = None
    args = sys.argv[1:]
    while args:
        arg = args.pop(0)
        if arg == "--data":
            data_dir = Path(args.pop(0))
        elif arg.startswith("--equity"):
            value = arg.split("=", 1)[1] if "=" in arg else args.pop(0)
            equity = float(value)
        elif arg == "--symbols":
            symbols = []
            while args and not args[0].startswith("--"):
                symbols.append(args.pop(0))
        else:
            print(f"unknown option {arg!r}"); return
    if data_dir is None:
        print("--data <dir> is required"); return

    frames = load(data_dir, symbols)
    if not frames:
        print("no data files found"); return
    print(f"loaded {len(frames)} symbols from {data_dir}")

    dates = sorted(set().union(*[set(df.index) for df in frames.values()]))
    split = dates[int(len(dates) * 0.6)]

    full = run(frames, equity)
    report(f"FULL PERIOD @ ${equity:,.2f}", full, equity)

    ins = run(frames, equity, end=split)
    report("IN-SAMPLE (first 60%)", ins, equity)

    oos = run(frames, equity, start=split)
    report("OUT-OF-SAMPLE (final 40%)", oos, equity)

    print("\n===== benchmarks (same window, buy once and hold, $1 commission) =====")
    for bench in ("SPY", "QQQ"):
        if bench in frames:
            net = buy_hold(frames, bench, equity)
            print(f"{bench} buy & hold: ${net:+.2f}  ({net / equity * 100:+.1f}%)")

    verdict_fail = []
    if oos["pf"] < 1.3:
        verdict_fail.append(f"OOS profit factor {oos['pf']:.2f} < 1.3")
    if oos["net"] <= 0:
        verdict_fail.append(f"OOS net ${oos['net']:+.2f} <= 0")
    if oos["max_dd"] > 15.0:
        verdict_fail.append(f"OOS max drawdown {oos['max_dd']:.1f}% > 15%")
    print()
    if verdict_fail:
        print("VERDICT: RED — " + "; ".join(verdict_fail))
        print("  Do not arm this live. Do not retune against the OOS window.")
        sys.exit(1)
    print("VERDICT: promotion gates PASS on the out-of-sample window.")


if __name__ == "__main__":
    main()
