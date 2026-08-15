"""Backtest the Medik multi-timeframe structure across the cached universe.

WHAT THIS RUNS, AND WHY IT IS AN UPPER BOUND
--------------------------------------------
The live strategy signals on 15-minute bars. Pulling 15m history for 200
symbols through the MCP connector is not practical, so this runs the SAME
structure with the DAILY bar as the entry timeframe:

    weekly regime BULLISH  +  daily regime BULLISH  +  entry-timeframe
    EMA/RSI momentum  ->  long, stop 1.5xATR, target 2R

That is deliberately favourable to the strategy. Trading the 15-minute
timeframe instead produces far MORE trades over the same period, and every
trade pays the same fixed-floor commission, so the intraday version's cost
drag is strictly worse. A structure that cannot clear costs here cannot
clear them at 15 minutes.

No-lookahead discipline:
  * weekly context uses only weeks that CLOSED before the signal date
  * the signal is computed on bar i and filled at bar i+1's OPEN
  * when a bar's range spans both stop and target, the STOP fills first

Usage:
    python backtest/run_medik_mtf.py [equity] [max_symbols]
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.medik_mtf import (
    ATR_STOP_MULTIPLIER,
    RISK_REWARD,
    OHLCV,
    atr,
    generate_signal,
)

PER_SHARE, MIN_COMMISSION, MAX_PCT = 0.005, 1.00, 0.01


def commission(shares: float, value: float) -> float:
    if shares <= 0 or value <= 0:
        return 0.0
    return min(max(PER_SHARE * shares, MIN_COMMISSION), MAX_PCT * value)


def load_symbol(path: Path):
    d = json.loads(path.read_text())
    need = ("time", "open", "high", "low", "close", "volume")
    if not all(k in d for k in need):
        return None
    lengths = {len(d[k]) for k in need}
    if len(lengths) != 1:          # ragged arrays — the data_quality.py lesson
        return None
    bars = [OHLCV(o, h, l, c, v) for o, h, l, c, v
            in zip(d["open"], d["high"], d["low"], d["close"], d["volume"])]
    return bars, [t[:10] for t in d["time"]]


def weekly_from_daily(bars, dates):
    """Aggregate daily bars into weekly, keeping each week's CLOSING date so
    the caller can exclude weeks that hadn't finished at signal time."""
    buckets: dict = {}
    for b, ds in zip(bars, dates):
        key = dt.date.fromisoformat(ds).isocalendar()[:2]
        buckets.setdefault(key, []).append((b, ds))
    out_bars, out_dates = [], []
    for key in sorted(buckets):
        group = buckets[key]
        out_bars.append(OHLCV(
            open=group[0][0].open,
            high=max(x[0].high for x in group),
            low=min(x[0].low for x in group),
            close=group[-1][0].close,
            volume=sum(x[0].volume for x in group),
        ))
        out_dates.append(group[-1][1])
    return out_bars, out_dates


def backtest_symbol(symbol, bars, dates, equity, risk_pct=1.0, position_pct=10.0):
    wbars, wdates = weekly_from_daily(bars, dates)
    atrs = atr(bars)
    avg_vol = sum(b.volume for b in bars[-60:]) / max(1, min(len(bars), 60))

    trades, evaluated, position = [], 0, None
    for i in range(260, len(bars) - 1):
        ds, nxt = dates[i], bars[i + 1]

        if position is not None:
            entry_px, shares, stop, target, entry_ds, entry_comm = position
            exit_px = reason = None
            if nxt.low <= stop:
                exit_px, reason = stop, "stop"
            elif nxt.high >= target:
                exit_px, reason = target, "target"
            if exit_px is not None:
                gross = (exit_px - entry_px) * shares
                trades.append({
                    "symbol": symbol, "entry_date": entry_ds, "exit_date": dates[i + 1],
                    "entry": entry_px, "exit": exit_px, "shares": shares, "reason": reason,
                    "gross": gross,
                    "commission": entry_comm + commission(shares, exit_px * shares),
                })
                position = None
            continue

        w_hist = [wb for wb, wd in zip(wbars, wdates) if wd < ds]
        if len(w_hist) < 40:
            continue

        evaluated += 1
        sig = generate_signal(
            symbol=symbol,
            weekly=w_hist,
            daily=bars[:i + 1],
            entry=bars[:i + 1],
            avg_daily_volume=avg_vol,
            volume_ratio=(bars[i].volume / (sum(b.volume for b in bars[i - 20:i]) / 20)
                          if i >= 20 and sum(b.volume for b in bars[i - 20:i]) > 0 else 1.0),
            account_can_short=False,
        )
        if sig.action != "BUY":
            continue

        fill = nxt.open
        a = atrs[i]
        stop = fill - a * ATR_STOP_MULTIPLIER
        target = fill + (fill - stop) * RISK_REWARD
        if stop <= 0 or fill <= stop:
            continue
        shares = min(equity * risk_pct / 100.0 / (fill - stop),
                     equity * position_pct / 100.0 / fill)
        if shares <= 0:
            continue
        position = (fill, shares, stop, target, ds, commission(shares, fill * shares))

    return trades, evaluated


def main() -> None:
    equity = float(sys.argv[1]) if len(sys.argv) > 1 else 294.0
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 10_000

    scratch = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    if scratch is None:
        import glob
        candidates = glob.glob("/tmp/claude-*/-home-user-Medik/*/scratchpad/data2y")
        if not candidates:
            print("no data2y cache found")
            sys.exit(1)
        scratch = Path(candidates[0])

    files = sorted(scratch.glob("*.json"))[:limit]
    all_trades, evaluated, skipped, used = [], 0, [], 0

    for path in files:
        loaded = load_symbol(path)
        if loaded is None:
            skipped.append(path.stem)
            continue
        bars, dates = loaded
        if len(bars) < 300:
            skipped.append(path.stem)
            continue
        trades, ev = backtest_symbol(path.stem, bars, dates, equity)
        all_trades.extend(trades)
        evaluated += ev
        used += 1

    gross = sum(t["gross"] for t in all_trades)
    comm = sum(t["commission"] for t in all_trades)
    net = gross - comm
    wins = [t for t in all_trades if t["gross"] - t["commission"] > 0]
    stops = [t for t in all_trades if t["reason"] == "stop"]

    def pf(key):
        g = sum(v for v in (t["gross"] - (t["commission"] if key == "net" else 0)
                            for t in all_trades) if v > 0)
        l = -sum(v for v in (t["gross"] - (t["commission"] if key == "net" else 0)
                             for t in all_trades) if v < 0)
        return g / l if l > 0 else float("inf")

    print("=" * 68)
    print("MEDIK MULTI-TIMEFRAME — DAILY-BAR UPPER BOUND, NET OF COMMISSIONS")
    print("=" * 68)
    print(f"symbols used        {used}   (skipped {len(skipped)}: {', '.join(skipped[:6])}"
          f"{'...' if len(skipped) > 6 else ''})")
    print(f"starting equity     ${equity:,.2f}")
    print(f"bars evaluated      {evaluated:,}")
    print(f"trades taken        {len(all_trades):,}")
    if not all_trades:
        print("\nno trades — nothing to measure")
        return
    print(f"win rate (net)      {len(wins) / len(all_trades):.1%}")
    print(f"stopped out         {len(stops) / len(all_trades):.1%}")
    print()
    print(f"  gross P&L         ${gross:+,.2f}")
    print(f"  commissions       ${-comm:,.2f}")
    print(f"  ---------------------------------")
    print(f"  NET P&L           ${net:+,.2f}   ({net / equity:+.1%} of starting equity)")
    print()
    print(f"  profit factor gross {pf('gross'):.2f}")
    print(f"  profit factor net   {pf('net'):.2f}")
    print(f"  avg gross/trade   ${gross / len(all_trades):+.3f}")
    print(f"  avg commission    ${comm / len(all_trades):.3f}")
    print(f"  avg position      ${sum(t['entry'] * t['shares'] for t in all_trades) / len(all_trades):,.2f}")
    print(f"  commission as % of position  "
          f"{comm / sum(t['entry'] * t['shares'] for t in all_trades):.2%}")


if __name__ == "__main__":
    main()
