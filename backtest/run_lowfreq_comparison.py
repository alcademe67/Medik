"""Compare low-frequency strategies against buy-and-hold, net of this
account's real commissions.

    python backtest/run_lowfreq_comparison.py [capital]

Reads the 5-year ETF cache written by the data-fetch agents. Every result
below is AFTER commissions -- the whole point of this comparison is that
gross numbers were never the problem.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from backtest.lowfreq import run_lowfreq
from backtest.strategies_lowfreq import (
    buy_and_hold,
    cross_sectional_momentum,
    dual_momentum,
    sma_timing,
)

DATA5Y = "/tmp/claude-0/-home-user-Medik/6e4758d5-550e-5d2f-aa54-83a8153cb3f0/scratchpad/data5y"
CAPITAL = float(sys.argv[1]) if len(sys.argv) > 1 else 300.43
COLS = ("open", "high", "low", "close", "volume")


def load(dirpath: str) -> dict:
    out = {}
    if not os.path.isdir(dirpath):
        return out
    for fn in sorted(os.listdir(dirpath)):
        if not fn.endswith(".json"):
            continue
        sym = fn[:-5]
        try:
            raw = json.load(open(os.path.join(dirpath, fn)))
            lens = {k: len(raw[k]) for k in COLS + ("time",)}
            if len(set(lens.values())) != 1:
                print(f"  (skipping {sym}: ragged arrays {lens})")
                continue
            idx = pd.to_datetime(raw["time"], utc=True).tz_localize(None)
            out[sym] = pd.DataFrame({k: raw[k] for k in COLS}, index=idx).sort_index()
        except Exception as exc:
            print(f"  (skipping {sym}: {exc})")
    return out


def show(rows: list) -> None:
    hdr = f"{'strategy':<34}{'total':>9}{'CAGR':>8}{'maxDD':>8}{'Sharpe':>8}{'fills':>7}{'comm$':>9}{'end$':>10}"
    print(hdr)
    print("-" * len(hdr))
    for s in rows:
        print(f"{s['name']:<34}{s['total_return_pct']:>8.1f}%{s['cagr_pct']:>7.1f}%"
              f"{s['max_drawdown_pct']:>7.1f}%{s['sharpe']:>8.2f}{s['fills']:>7}"
              f"{s['commission']:>9.2f}{s['end']:>10.2f}")


def main() -> None:
    etfs = load(DATA5Y)
    if not etfs:
        raise SystemExit(f"no data in {DATA5Y}")
    span = next(iter(etfs.values()))
    print(f"Loaded {len(etfs)} ETFs: {sorted(etfs)}")
    print(f"Span: {span.index[0].date()} .. {span.index[-1].date()} ({len(span)} bars)")
    print(f"Capital: ${CAPITAL:,.2f}   (all results NET of real commissions)\n")

    results = []
    risk_assets = [s for s in ("SPY", "QQQ", "IWM", "EFA") if s in etfs]
    safe = "TLT" if "TLT" in etfs else None

    if "SPY" in etfs:
        results.append(run_lowfreq("SPY buy & hold", {"SPY": etfs["SPY"]},
                                   buy_and_hold("SPY"), CAPITAL, freq="never").summary())
    if "QQQ" in etfs:
        results.append(run_lowfreq("QQQ buy & hold", {"QQQ": etfs["QQQ"]},
                                   buy_and_hold("QQQ"), CAPITAL, freq="never").summary())

    if "SPY" in etfs:
        results.append(run_lowfreq("SPY 200d timing (monthly)", {"SPY": etfs["SPY"]},
                                   sma_timing("SPY"), CAPITAL, freq="M").summary())
        if safe:
            results.append(run_lowfreq("SPY 200d timing -> TLT",
                                       {k: etfs[k] for k in ("SPY", "TLT")},
                                       sma_timing("SPY", safe="TLT"), CAPITAL, freq="M").summary())
    if "QQQ" in etfs:
        results.append(run_lowfreq("QQQ 200d timing (monthly)", {"QQQ": etfs["QQQ"]},
                                   sma_timing("QQQ"), CAPITAL, freq="M").summary())

    if len(risk_assets) >= 2:
        results.append(run_lowfreq(f"dual momentum ({'/'.join(risk_assets)})", etfs,
                                   dual_momentum(risk_assets, safe), CAPITAL, freq="M").summary())
        results.append(run_lowfreq("dual momentum (quarterly)", etfs,
                                   dual_momentum(risk_assets, safe), CAPITAL, freq="Q").summary())

    results.sort(key=lambda r: -r["cagr_pct"])
    print("=" * 93)
    print("ETF STRATEGIES — 5 YEARS, NET OF COMMISSIONS")
    print("=" * 93)
    show(results)

    # Cross-sectional stock momentum, if the 2y stock cache is present.
    data2y = DATA5Y.replace("data5y", "data2y")
    stocks = load(data2y)
    if len(stocks) > 20:
        print(f"\nLoaded {len(stocks)} stocks from the 2y cache for cross-sectional momentum.")
        if "SPY" in etfs:
            stocks["SPY"] = etfs["SPY"]
        stock_rows = []
        for n in (3, 5):
            for freq, label in (("M", "monthly"), ("Q", "quarterly")):
                r = run_lowfreq(
                    f"x-sec momentum top{n} ({label})", stocks,
                    cross_sectional_momentum(top_n=n, trend_filter_symbol="SPY" if "SPY" in stocks else None),
                    CAPITAL, freq=freq, warmup_bars=300, max_weight_per_position=0.20 if n < 5 else None,
                ).summary()
                stock_rows.append(r)
        span2 = next(iter(stocks.values()))
        print(f"Span: {span2.index[0].date()} .. {span2.index[-1].date()}"
              f"  (SHORT sample — treat as indicative only)\n")
        print("=" * 93)
        print("STOCK CROSS-SECTIONAL MOMENTUM — ~2 YEARS, NET OF COMMISSIONS")
        print("=" * 93)
        stock_rows.sort(key=lambda r: -r["cagr_pct"])
        show(stock_rows)


if __name__ == "__main__":
    main()
