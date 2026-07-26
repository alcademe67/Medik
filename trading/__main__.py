"""Command-line entry for the research engine.

    python -m trading scan                     # rank liquid USDT pairs
    python -m trading backtest BTC/USDT 1h      # backtest one pair
    python -m trading backtest-top 1h           # backtest the top scanner hits
    python -m trading compare BTC/USDT 1d       # compare every strategy on one pair
    python -m trading validate BTC/USDT 1d      # in-sample vs out-of-sample (overfit check)

All read-only. No orders are ever placed.
"""

import logging
import sys

from trading import backtest, config, scanner
from trading.exchange import Exchange, ExchangeError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _cmd_scan() -> None:
    ex = Exchange()
    ranked = scanner.scan(ex)
    print(f"\nTop {len(ranked)} liquid {config.QUOTE_CURRENCY} pairs by 24h volume:\n")
    for _, r in ranked.iterrows():
        vol = r["quote_volume"] or 0
        print(f"  {r['symbol']:<14} vol ${vol:,.0f}   last {r['last']}")


def _cmd_backtest(symbol: str, timeframe: str) -> None:
    ex = Exchange()
    df = ex.fetch_ohlcv(symbol, timeframe)
    result = backtest.run(
        df,
        timeframe=timeframe,
        initial_cash=config.INITIAL_CASH,
        fee_rate=config.FEE_RATE,
    )
    print("\n" + backtest.format_report(symbol, result))


def _cmd_backtest_top(timeframe: str) -> None:
    ex = Exchange()
    ranked = scanner.scan(ex)
    print(f"\nBacktesting top {len(ranked)} pairs @ {timeframe}...\n")
    rows = []
    for _, r in ranked.iterrows():
        symbol = r["symbol"]
        try:
            res = backtest.run(ex.fetch_ohlcv(symbol, timeframe), timeframe=timeframe)
        except ExchangeError as exc:
            print(f"  {symbol:<14} skipped ({exc})")
            continue
        rows.append((symbol, res))
        print(
            f"  {symbol:<14} return {res.total_return_pct:+7.2f}%  "
            f"Sharpe {res.sharpe:5.2f}  trades {res.num_trades:3d}  "
            f"maxDD {res.max_drawdown_pct:6.2f}%"
        )
    if rows:
        best = max(rows, key=lambda x: x[1].sharpe)
        print(f"\nBest by Sharpe: {best[0]} ({best[1].sharpe:.2f})")


def _cmd_compare(symbol: str, timeframe: str) -> None:
    """Backtest every strategy on one pair and report the metrics side by
    side — so the data picks the winner, not a guess. Use a long timeframe
    (e.g. 1d) for multi-year coverage."""
    ex = Exchange()
    df = ex.fetch_ohlcv(symbol, timeframe)
    print(f"\nStrategy comparison — {symbol} @ {timeframe}  ({len(df)} bars)\n")
    print(f"  {'strategy':<10}{'net %':>9}{'win %':>8}{'PF':>7}{'maxDD %':>9}{'trades':>8}")
    print("  " + "-" * 51)
    original, results = config.STRATEGY, []
    for name in ("trend", "meanrev", "breakout", "regime"):
        config.STRATEGY = name
        res = backtest.run(
            df, timeframe=timeframe, initial_cash=config.INITIAL_CASH, fee_rate=config.FEE_RATE
        )
        results.append((name, res))
        pf = " inf" if res.profit_factor == float("inf") else f"{res.profit_factor:.2f}"
        print(
            f"  {name:<10}{res.total_return_pct:>+8.2f}%{res.win_rate_pct:>7.1f}%"
            f"{pf:>7}{res.max_drawdown_pct:>8.2f}%{res.num_trades:>8}"
        )
    config.STRATEGY = original

    traded = [(n, r) for n, r in results if r.num_trades > 0]
    if traded:
        best = max(traded, key=lambda x: x[1].total_return_pct)
        print(f"\n  Best net return: {best[0]} ({best[1].total_return_pct:+.2f}%)")
    print("\n  Research only — past performance does not predict the future.")


def _cmd_validate(symbol: str, timeframe: str) -> None:
    """Split history into in-sample (first 70%) and out-of-sample (last 30%),
    backtest each strategy on both, and flag over-fitting. A real edge holds
    up on data the strategy never saw; in-sample-only gains are a mirage."""
    ex = Exchange()
    df = ex.fetch_ohlcv(symbol, timeframe)
    if len(df) < 100:
        raise SystemExit("not enough history to split for validation")
    split = int(len(df) * 0.7)
    is_df, oos_df = df.iloc[:split], df.iloc[split:]

    print(f"\nWalk-forward check — {symbol} @ {timeframe}")
    print(f"  in-sample {len(is_df)} bars  |  out-of-sample {len(oos_df)} bars\n")
    print(f"  {'strategy':<10}{'IS net%':>9}{'OOS net%':>10}{'OOS PF':>8}   verdict")
    print("  " + "-" * 58)
    original = config.STRATEGY
    for name in ("trend", "meanrev", "breakout", "regime"):
        config.STRATEGY = name
        isr = backtest.run(is_df, timeframe=timeframe, fee_rate=config.FEE_RATE)
        oos = backtest.run(oos_df, timeframe=timeframe, fee_rate=config.FEE_RATE)
        if isr.total_return_pct <= 0:
            verdict = "no edge (fails in-sample)"
        elif oos.total_return_pct > 0:
            verdict = "holds up out-of-sample"
        else:
            verdict = "OVERFIT (fails out-of-sample)"
        oos_pf = "inf" if oos.profit_factor == float("inf") else f"{oos.profit_factor:.2f}"
        print(
            f"  {name:<10}{isr.total_return_pct:>+8.2f}%{oos.total_return_pct:>+9.2f}%"
            f"{oos_pf:>8}   {verdict}"
        )
    config.STRATEGY = original
    print("\n  A real edge survives on unseen data. In-sample-only gains usually don't repeat.")


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "scan"
    try:
        if cmd == "scan":
            _cmd_scan()
        elif cmd == "backtest":
            if len(args) < 2:
                raise SystemExit("usage: python -m trading backtest SYMBOL [TIMEFRAME]")
            _cmd_backtest(args[1], args[2] if len(args) > 2 else config.TIMEFRAME)
        elif cmd == "backtest-top":
            _cmd_backtest_top(args[1] if len(args) > 1 else config.TIMEFRAME)
        elif cmd == "compare":
            if len(args) < 2:
                raise SystemExit("usage: python -m trading compare SYMBOL [TIMEFRAME]")
            _cmd_compare(args[1], args[2] if len(args) > 2 else config.TIMEFRAME)
        elif cmd == "validate":
            if len(args) < 2:
                raise SystemExit("usage: python -m trading validate SYMBOL [TIMEFRAME]")
            _cmd_validate(args[1], args[2] if len(args) > 2 else config.TIMEFRAME)
        else:
            raise SystemExit(f"unknown command: {cmd}")
    except ExchangeError as exc:
        raise SystemExit(f"Exchange error: {exc}")


if __name__ == "__main__":
    main()
