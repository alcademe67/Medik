"""Compare low-frequency strategies against buy-and-hold, net of this
account's real commissions.

    python backtest/run_lowfreq_comparison.py [capital] [--save]

Reads the 5-year ETF cache from data/data5y and the 2-year stock cache from
data/data2y (override with $MEDIK_ETF_CACHE / $MEDIK_BAR_CACHE). Fill them
with `python examples/fetch_bar_cache.py`. --save also writes the output to
reports/.

Every result below is AFTER commissions -- the whole point of this comparison
is that gross numbers were never the problem.
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.lowfreq import run_lowfreq
from backtest.strategies_lowfreq import (
    buy_and_hold,
    cross_sectional_momentum,
    dual_momentum,
    sma_timing,
)
from ibkr.cache import load_cache
from paths import bar_cache_dir, report_path

_ARGS = [a for a in sys.argv[1:] if not a.startswith("-")]
_FLAGS = {a for a in sys.argv[1:] if a.startswith("-")}
CAPITAL = float(_ARGS[0]) if _ARGS else 300.43
SAVE = "--save" in _FLAGS

ETF_CACHE = os.environ.get("MEDIK_ETF_CACHE", "data5y")
STOCK_CACHE = os.environ.get("MEDIK_BAR_CACHE", "data2y")


def load(cache_name: str) -> dict:
    """Load a named cache, reporting anything skipped rather than silently
    comparing strategies over a smaller universe than intended."""
    frames, skipped = load_cache(bar_cache_dir(cache_name))
    for sym, why in skipped:
        print(f"  (skipping {sym}: {why})")
    return frames


def show(rows: list) -> None:
    hdr = f"{'strategy':<34}{'total':>9}{'CAGR':>8}{'maxDD':>8}{'Sharpe':>8}{'fills':>7}{'comm$':>9}{'end$':>10}"
    print(hdr)
    print("-" * len(hdr))
    for s in rows:
        print(f"{s['name']:<34}{s['total_return_pct']:>8.1f}%{s['cagr_pct']:>7.1f}%"
              f"{s['max_drawdown_pct']:>7.1f}%{s['sharpe']:>8.2f}{s['fills']:>7}"
              f"{s['commission']:>9.2f}{s['end']:>10.2f}")


def main() -> None:
    etfs = load(ETF_CACHE)
    if not etfs:
        raise SystemExit(
            f"no usable bars in {bar_cache_dir(ETF_CACHE)}\n"
            f'  Populate it first:  python examples/fetch_bar_cache.py {ETF_CACHE} --etfs --duration "5 Y"\n'
            f"  (needs TWS open and logged in)"
        )
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
    stocks = load(STOCK_CACHE)
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


class _Tee(io.StringIO):
    """Captures output for the report file while still printing it live."""

    def write(self, s: str) -> int:
        sys.__stdout__.write(s)
        return super().write(s)


if __name__ == "__main__":
    if SAVE:
        buffer = _Tee()
        with redirect_stdout(buffer):
            main()
        destination = report_path("lowfreq-comparison", "txt")
        destination.write_text(buffer.getvalue(), encoding="utf-8")
        print(f"\nsaved -> {destination}")
    else:
        main()
