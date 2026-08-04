"""Print recent scan candidates, trades, and summary stats from journal.sqlite.

No TWS connection needed -- this just reads the local database that
run_scan.py (and, going forward, the live trading loop) writes to.

Usage:
    python examples/show_journal.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy import journal


def main() -> None:
    stats = journal.summary_stats()
    print("=== Summary ===")
    print(f"Closed trades: {stats['closed_trades']}  Wins: {stats['wins']}  "
          f"Win rate: {stats['win_rate']:.0%}" if stats["closed_trades"] else "No closed trades yet.")
    print(f"Total realized P&L: ${stats['total_realized_pnl']:.2f}")

    print("\n=== Recent candidates (last 25) ===")
    for c in journal.recent_candidates(25):
        flag = "PASS" if c["passed"] else "fail"
        score = f"score {c['score']:.1f}" if c["score"] is not None else ""
        print(f"  [{c['checked_at']}] {c['symbol']:6} {flag:4} {c['side'] or '-':4} {score:12} {c['reason']}")

    print("\n=== Recent trades (last 25) ===")
    for t in journal.recent_trades(25):
        exit_info = f"-> {t['exit_price']} ({t['exit_reason']}) pnl {t['realized_pnl']}" if t["exit_price"] else "OPEN"
        print(f"  {t['symbol']:6} {t['side']:4} {t['quantity']} @ {t['entry_price']}  {exit_info}")


if __name__ == "__main__":
    main()
