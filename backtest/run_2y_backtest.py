"""Backtest over the 2-year cache.

Usage: run_2y_backtest.py <capital> <gate|pullback|both>
Long-only (matches the owner's TFSA), fractional sizing, full risk engine.

Reads data/data2y (override with $MEDIK_BAR_CACHE). Fill it first with
`python examples/fetch_bar_cache.py data2y --universe`.

NOTE: this reports GROSS results. Both strategies are net-negative once this
account's real commissions are applied -- run backtest/net_of_commission.py
before drawing any conclusion from the numbers below.
"""
import os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.engine import run_backtest
from ibkr.cache import require_cache
from paths import bar_cache_dir
from strategy.config import DEFAULT_CONFIG
from strategy.data_quality import drop_incomplete_trailing_bar

DATA_DIR = bar_cache_dir(os.environ.get("MEDIK_BAR_CACHE", "data2y"))
CAPITAL = float(sys.argv[1]) if len(sys.argv) > 1 else 300.43
SOURCE = sys.argv[2] if len(sys.argv) > 2 else "pullback"

price_data, skipped = require_cache(
    DATA_DIR,
    min_bars=DEFAULT_CONFIG.warmup_bars + 1,
    transform=lambda df: drop_incomplete_trailing_bar(df)[0],
)

span = None
if price_data:
    any_df = next(iter(price_data.values()))
    span = (any_df.index[0].date(), any_df.index[-1].date())

res = run_backtest(
    price_data, starting_capital=CAPITAL, config=DEFAULT_CONFIG,
    fractional=True, enforce_score_threshold=True, enforce_risk_limits=True,
    signal_source=SOURCE, long_only=True,
)
s = res.summary()

print("=" * 72)
print("BACKTEST  source=%s  long_only=True  %d symbols  %s..%s"
      % (SOURCE, len(price_data), span[0] if span else "?", span[1] if span else "?"))
print("capital $%s | risk/trade %.0f%% | gate score>=%.0f | pullback R:R>=%.0f"
      % (format(CAPITAL, ",.2f"), DEFAULT_CONFIG.max_risk_pct * 100,
         __import__("strategy.scoring", fromlist=["x"]).SCORE_THRESHOLD,
         DEFAULT_CONFIG.pullback_min_rr))
print("=" * 72)

if not s.get("trades"):
    print("  NO TRADES")
else:
    print("  trades:          %d  (wins %d / losses %d)" % (s["trades"], s["wins"], s["losses"]))
    print("  win_rate:        %.1f%%" % (s["win_rate"] * 100))
    print("  avg R/trade:     %+.3fR" % s["avg_r_multiple"])
    print("  profit_factor:   %.2f" % s["profit_factor"])
    print("  total_return:    %+.2f%%" % s["total_return_pct"])
    print("  max_drawdown:    %.2f%%" % s["max_drawdown_pct"])
    print("  ending_capital:  $%s" % format(s["ending_capital"], ",.2f"))

    by = {}
    for t in res.closed_trades:
        b = by.setdefault(t.strategy, {"n": 0, "w": 0, "pnl": 0.0, "r": 0.0})
        b["n"] += 1
        b["w"] += 1 if t.realized_pnl > 0 else 0
        b["pnl"] += t.realized_pnl
        b["r"] += t.r_multiple
    print("\n  per-strategy:")
    for k, v in sorted(by.items()):
        print("    %-9s n=%-4d win=%.0f%%  totalPnL=%+8.2f  avgR=%+.3f"
              % (k, v["n"], 100.0 * v["w"] / v["n"], v["pnl"], v["r"] / v["n"]))

    reasons = {}
    for t in res.closed_trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    print("  exit reasons:", dict(sorted(reasons.items())))

    print("\nClosed trades:")
    for t in sorted(res.closed_trades, key=lambda x: x.entry_date):
        print("  %-6s %-8s %10.5g @ %8.2f (%s) -> %8.2f %-13s pnl %+8.2f (%+.2fR)"
              % (t.symbol, t.strategy, t.quantity, t.entry_price, t.entry_date.date(),
                 t.exit_price, t.exit_reason, t.realized_pnl, t.r_multiple))

if skipped:
    print("\nSkipped (%d): %s" % (len(skipped), ", ".join("%s(%s)" % x for x in skipped)))
