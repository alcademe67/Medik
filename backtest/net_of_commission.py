"""Re-run the pullback backtest and apply the account's REAL commission
schedule, derived from 90 days of actual fills on this account:

    commission = clamp($0.005/share, min $1.00, max 1% of trade value)

Verified against real fills: JPM 0.09sh/$31.35 -> $0.3135 (1% cap binds),
NFLX 1sh/$67.13 -> $0.6713 (1% cap), F 9sh/$129.02 -> $1.0000 ($1 min),
MARA 16sh/$197.84 -> $1.0000, S 12sh/$232.67 -> $1.0000.

At this account's position sizes (~$30-60) the 1% cap always binds, so
every round trip costs ~2% of position value.

This is the gate every strategy change must pass before anyone calls it
working (CLAUDE.md). Reads data/data2y -- override with $MEDIK_BAR_CACHE.

    python backtest/net_of_commission.py <capital> <gate|pullback|both>
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

PER_SHARE = 0.005
MIN_COMM = 1.00
MAX_PCT = 0.01


def commission(shares: float, value: float) -> float:
    raw = max(PER_SHARE * shares, MIN_COMM)
    return min(raw, MAX_PCT * value)


price_data, skipped = require_cache(
    DATA_DIR,
    min_bars=DEFAULT_CONFIG.warmup_bars + 1,
    transform=lambda df: drop_incomplete_trailing_bar(df)[0],
)

res = run_backtest(
    price_data, starting_capital=CAPITAL, config=DEFAULT_CONFIG,
    fractional=True, enforce_score_threshold=True, enforce_risk_limits=True,
    signal_source=SOURCE, long_only=True,
)

closed = res.closed_trades
gross = sum(t.realized_pnl for t in closed)
total_comm = 0.0
for t in closed:
    entry_val = t.quantity * t.entry_price
    exit_val = t.quantity * (t.exit_price or t.entry_price)
    total_comm += commission(t.quantity, entry_val) + commission(t.quantity, exit_val)

net = gross - total_comm
wins = sum(1 for t in closed if t.realized_pnl > 0)

print("=" * 66)
print("NET-OF-COMMISSION RESULT  source=%s  %d symbols  2y" % (SOURCE, len(price_data)))
print("=" * 66)
print("  trades:              %d  (win rate %.1f%%)" % (len(closed), 100.0 * wins / len(closed) if closed else 0))
print("  GROSS profit:        $%+.2f   (%+.2f%% on $%.2f)" % (gross, 100 * gross / CAPITAL, CAPITAL))
print("  commissions paid:    $%.2f   (%d fills, avg $%.3f/fill)"
      % (total_comm, 2 * len(closed), total_comm / (2 * len(closed)) if closed else 0))
print("  NET profit:          $%+.2f   (%+.2f%% on $%.2f)" % (net, 100 * net / CAPITAL, CAPITAL))
print()
avg_pos = sum(t.quantity * t.entry_price for t in closed) / len(closed) if closed else 0
print("  avg position size:   $%.2f" % avg_pos)
print("  commission drag:     %.2f%% of position value per round trip"
      % (100 * (total_comm / len(closed)) / avg_pos if closed and avg_pos else 0))
print("  breakeven needs:     gross profit > $%.2f  (actual: $%.2f)" % (total_comm, gross))
