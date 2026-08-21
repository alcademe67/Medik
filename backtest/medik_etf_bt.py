"""Offline backtest of the MEDIK ETF strategy against cached intraday bars.

Reads data/etf_intraday/<SYMBOL>/*.json (written by
examples/fetch_etf_intraday.py) and runs strategy.medik_etf EXACTLY as
implemented -- this module imports score_candidate/size_trade/should_exit
rather than reimplementing them, so the thing measured is the thing that
would trade.

    python backtest/medik_etf_bt.py                    # all cached symbols
    python backtest/medik_etf_bt.py TQQQ SOXL          # a subset
    python backtest/medik_etf_bt.py --equity 290       # starting capital

EXECUTION REALISM
    * entry fills at the NEXT 5m bar's open, plus half the spread and
      slippage -- never at the signal bar's close
    * the stop is a STOP order: when the bar's low touches it the fill is
      the stop price MINUS slippage, and if the bar gapped through it the
      fill is the open. Stops do not fill at the stop price in a fast market.
    * the target is a LIMIT: it fills at the target with no improvement
    * when one bar spans both stop and target, the STOP is assumed first --
      intrabar order is unknowable from OHLC, and assuming the good outcome
      is how backtests flatter themselves
    * commission per fill is this account's real schedule:
      clamp($0.005/share, min $1.00, max 1% of trade value)
    * whole shares only, via the strategy's own size_trade()

NOT MODELLED (each flatters the strategy, so results are optimistic)
    * partial fills and queue position on the limit entry
    * borrow/short costs -- irrelevant here, every trade is a BUY
    * the leveraged funds' daily reset decay within a holding period
"""
from __future__ import annotations

import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.medik_etf import (
    MAX_DAILY_LOSS_PCT,
    MAX_TRADES_PER_SESSION,
    OpenTrade,
    PortfolioState,
    Position,
    SessionControls,
    SizingRejected,
    ETFSnapshot,
    profile_for,
    rank_candidates,
    score_candidate,
    should_exit,
    size_trade,
    within_trading_window,
)
from strategy.medik_mtf import OHLCV

DATA_ROOT = Path(__file__).resolve().parent.parent / "data" / "etf_intraday"

PER_SHARE, MIN_COMMISSION, MAX_PCT = 0.005, 1.00, 0.01
SLIPPAGE_BPS = 2.0          # 0.02% adverse on market-ish fills
STOP_SLIPPAGE_BPS = 5.0     # stops fill worse than the trigger
# Half-spread assumption by leverage; leveraged funds quote wider.
SPREAD_BPS = {1.0: 1.0, 2.0: 3.0, 3.0: 4.0}

BARS_PER_SESSION = 78       # 09:30-16:00 in 5-minute bars


def commission(shares: float, value: float) -> float:
    if shares <= 0 or value <= 0:
        return 0.0
    return min(max(PER_SHARE * shares, MIN_COMMISSION), MAX_PCT * value)


def half_spread(symbol: str, price: float) -> float:
    bps = SPREAD_BPS.get(profile_for(symbol).leverage, 4.0)
    return price * bps / 10_000.0 / 2.0


# ------------------------------------------------------------------- loading


def load_symbol(symbol: str, root: Path = DATA_ROOT):
    """Load and merge every cached chunk for one symbol, de-duplicated."""
    folder = root / symbol
    if not folder.exists():
        return None
    merged: dict[str, dict] = {}
    for path in sorted(folder.glob("*.json")):
        raw = json.loads(path.read_text())
        times = raw.get("time") or []
        for i, ts in enumerate(times):
            merged[ts] = {k: raw[k][i] for k in ("open", "high", "low", "close", "volume")}
    if not merged:
        return None
    times = sorted(merged)
    bars = [OHLCV(merged[t]["open"], merged[t]["high"], merged[t]["low"],
                  merged[t]["close"], float(merged[t]["volume"] or 0)) for t in times]
    return bars, times


def to_15m(bars, times):
    """Derive 15m bars from 5m by grouping threes WITHIN a session.

    Groups are aligned to the session start, and a trailing partial group is
    dropped: an incomplete 15m bar is not a completed observation.
    """
    out_bars, out_times, session = [], [], None
    bucket = []
    for b, t in zip(bars, times):
        day = t[:10]
        if day != session:
            if len(bucket) == 3:
                out_bars.append(_merge(bucket)); out_times.append(bucket[-1][1])
            session, bucket = day, []
        bucket.append((b, t))
        if len(bucket) == 3:
            out_bars.append(_merge(bucket)); out_times.append(bucket[-1][1])
            bucket = []
    return out_bars, out_times


def _merge(group):
    bars = [g[0] for g in group]
    return OHLCV(bars[0].open, max(b.high for b in bars), min(b.low for b in bars),
                 bars[-1].close, sum(b.volume for b in bars))


# -------------------------------------------------------------------- result


@dataclass
class SymbolResult:
    symbol: str
    trades: list = field(default_factory=list)
    skips_whole_share: int = 0
    skips_other: int = 0
    signals: int = 0
    sessions: int = 0

    @property
    def net(self):
        return sum(t["net"] for t in self.trades)

    @property
    def wins(self):
        return [t for t in self.trades if t["net"] > 0]

    @property
    def losses(self):
        return [t for t in self.trades if t["net"] <= 0]

    def summary(self, equity: float) -> dict:
        n = len(self.trades)
        gross = sum(t["gross"] for t in self.trades)
        comm = sum(t["commission"] for t in self.trades)
        avg_win = statistics.mean([t["net"] for t in self.wins]) if self.wins else 0.0
        avg_loss = statistics.mean([t["net"] for t in self.losses]) if self.losses else 0.0
        gains = sum(t["net"] for t in self.wins)
        pains = -sum(t["net"] for t in self.losses)
        return {
            "symbol": self.symbol, "trades": n, "sessions": self.sessions,
            "signals": self.signals,
            "skips_whole_share": self.skips_whole_share,
            "skips_other": self.skips_other,
            "win_rate": len(self.wins) / n if n else 0.0,
            "gross": gross, "commission": comm, "net": self.net,
            "avg_win": avg_win, "avg_loss": avg_loss,
            "profit_factor": (gains / pains) if pains > 0 else (float("inf") if gains else 0.0),
            "expectancy": self.net / n if n else 0.0,
            "largest_loss": min([t["net"] for t in self.trades], default=0.0),
            "largest_win": max([t["net"] for t in self.trades], default=0.0),
            "avg_duration_bars": statistics.mean([t["bars_held"] for t in self.trades]) if n else 0.0,
            "avg_notional": statistics.mean([t["notional"] for t in self.trades]) if n else 0.0,
            "return_pct": self.net / equity * 100.0 if equity else 0.0,
        }


def backtest_symbol(symbol: str, bars, times, equity: float,
                    start_idx: int = 0, end_idx: int | None = None) -> SymbolResult:
    """Single-symbol pass. One position at a time, session controls applied."""
    res = SymbolResult(symbol)
    bars15, times15 = to_15m(bars, times)
    end_idx = len(bars) - 1 if end_idx is None else min(end_idx, len(bars) - 1)

    session = None
    controls = None
    session_start_equity = equity
    open_trade: OpenTrade | None = None
    entry_fill = 0.0
    entry_comm = 0.0
    entry_i = 0
    running = equity

    for i in range(start_idx, end_idx):
        ts = times[i]
        day = ts[:10]
        if day != session:
            session = day
            res.sessions += 1
            session_start_equity = running
            controls = SessionControls(equity_start_of_session=running)

        bar_index_in_session = None
        # 5m bar k of the session -> minutes since 09:30
        # times are UTC; convert by counting bars from the session's first
        j = i
        count = 0
        while j > 0 and times[j - 1][:10] == day:
            j -= 1; count += 1
        bar_index_in_session = count
        now_minutes = 9 * 60 + 30 + bar_index_in_session * 5

        nxt = bars[i + 1]

        # ---- manage an open position on the NEXT bar
        if open_trade is not None:
            exit_px = reason = None
            if nxt.open <= open_trade.stop:                 # gapped through
                exit_px, reason = nxt.open, "stop_gap"
            elif nxt.low <= open_trade.stop:
                exit_px = open_trade.stop * (1 - STOP_SLIPPAGE_BPS / 10_000.0)
                reason = "stop"
            elif nxt.high >= open_trade.target:
                exit_px, reason = open_trade.target, "target"
            elif times[i + 1][:10] != day:                  # session rolled
                exit_px, reason = bars[i].close, "session_close"

            if exit_px is not None:
                exit_px -= half_spread(symbol, exit_px)
                gross = (exit_px - entry_fill) * open_trade.quantity
                exit_comm = commission(open_trade.quantity, exit_px * open_trade.quantity)
                net = gross - entry_comm - exit_comm
                running += net
                res.trades.append({
                    "symbol": symbol, "entry_time": times[entry_i], "exit_time": times[i + 1],
                    "entry": entry_fill, "exit": exit_px, "qty": open_trade.quantity,
                    "reason": reason, "gross": gross,
                    "commission": entry_comm + exit_comm, "net": net,
                    "bars_held": (i + 1) - entry_i,
                    "notional": entry_fill * open_trade.quantity,
                })
                controls.trades_completed += 1
                open_trade = None
            continue

        # ---- entry search
        ok, _ = within_trading_window(now_minutes)
        if not ok:
            continue
        if controls.trades_completed >= MAX_TRADES_PER_SESSION:
            continue
        if session_start_equity > 0 and \
                (session_start_equity - running) / session_start_equity * 100 >= MAX_DAILY_LOSS_PCT:
            continue

        session_bars = bars[max(0, i - bar_index_in_session):i + 1]
        if len(session_bars) < 25:
            continue
        hist15 = [b for b, t in zip(bars15, times15) if t <= ts]
        if len(hist15) < 25:
            continue

        price = bars[i].close
        hs = half_spread(symbol, price)
        snap = ETFSnapshot(
            symbol=symbol, price=price, bid=price - hs, ask=price + hs,
            bars_5m=session_bars, bars_15m=hist15,
            session_dollar_volume=sum(b.close * b.volume for b in session_bars),
        )
        cs = score_candidate(snap)
        if cs.signal != "TRADE":
            continue
        res.signals += 1

        state = PortfolioState(running, running, (), 0)
        try:
            sized = size_trade(cs, state)
        except SizingRejected as exc:
            if "below 1 whole share" in str(exc):
                res.skips_whole_share += 1
            else:
                res.skips_other += 1
            continue

        fill = nxt.open * (1 + SLIPPAGE_BPS / 10_000.0) + half_spread(symbol, nxt.open)
        stop_d = sized.entry - sized.stop
        stop = fill - stop_d
        target = fill + stop_d * 1.5
        entry_fill = fill
        entry_comm = commission(sized.quantity, fill * sized.quantity)
        entry_i = i + 1
        open_trade = OpenTrade(symbol, sized.quantity, fill, stop, target, 0.0)

    return res


# ---------------------------------------------------------------- reporting


def _fmt(summary: dict) -> str:
    s = summary
    pf = s["profit_factor"]
    pf_txt = "inf" if pf == float("inf") else f"{pf:.2f}"
    return (f"{s['symbol']:<6}{s['trades']:>7}{s['win_rate']:>8.0%}"
            f"{s['gross']:>+10.2f}{s['commission']:>10.2f}{s['net']:>+10.2f}"
            f"{pf_txt:>8}{s['expectancy']:>+11.3f}{s['skips_whole_share']:>8}"
            f"{s['signals']:>9}")


def report(results: list[SymbolResult], equity: float, label: str) -> dict:
    print(f"\n{'=' * 92}")
    print(f"{label}")
    print("=" * 92)
    print(f"{'ETF':<6}{'trades':>7}{'win%':>8}{'gross':>10}{'comm':>10}"
          f"{'net':>10}{'PF':>8}{'expect':>11}{'wsSkip':>8}{'signals':>9}")
    print("-" * 92)
    for r in sorted(results, key=lambda x: -x.net):
        if r.trades or r.signals:
            print(_fmt(r.summary(equity)))

    all_trades = [t for r in results for t in r.trades]
    gross = sum(t["gross"] for t in all_trades)
    comm = sum(t["commission"] for t in all_trades)
    net = gross - comm
    wins = [t for t in all_trades if t["net"] > 0]
    losses = [t for t in all_trades if t["net"] <= 0]
    gains = sum(t["net"] for t in wins)
    pains = -sum(t["net"] for t in losses)
    ws_skips = sum(r.skips_whole_share for r in results)
    signals = sum(r.signals for r in results)
    sessions = max((r.sessions for r in results), default=0)

    # equity curve / drawdown, trades in chronological order
    curve, peak, maxdd = equity, equity, 0.0
    for t in sorted(all_trades, key=lambda x: x["entry_time"]):
        curve += t["net"]
        peak = max(peak, curve)
        maxdd = max(maxdd, (peak - curve) / peak if peak > 0 else 0.0)

    print("-" * 92)
    print(f"COMBINED  trades {len(all_trades)}   signals {signals}   "
          f"whole-share skips {ws_skips}   sessions {sessions}")
    if all_trades:
        print(f"  win rate        {len(wins) / len(all_trades):.1%}   "
              f"({len(wins)}W / {len(losses)}L)")
        print(f"  avg win         ${statistics.mean([t['net'] for t in wins]):+.3f}"
              if wins else "  avg win         n/a")
        print(f"  avg loss        ${statistics.mean([t['net'] for t in losses]):+.3f}"
              if losses else "  avg loss        n/a")
        print(f"  gross P&L       ${gross:+,.2f}")
        print(f"  commissions     ${-comm:,.2f}")
        print(f"  NET P&L         ${net:+,.2f}   ({net / equity:+.1%} of ${equity:,.0f})")
        pf = gains / pains if pains > 0 else float("inf")
        print(f"  profit factor   {'inf' if pf == float('inf') else f'{pf:.2f}'}"
              f"   (gross {(sum(t['gross'] for t in wins) / -sum(t['gross'] for t in losses)):.2f})"
              if losses and sum(t['gross'] for t in losses) else f"  profit factor   {pf}")
        print(f"  expectancy      ${net / len(all_trades):+.3f} per trade")
        print(f"  max drawdown    {maxdd:.1%}")
        print(f"  largest loss    ${min(t['net'] for t in all_trades):+.2f}")
        print(f"  largest win     ${max(t['net'] for t in all_trades):+.2f}")
        print(f"  avg notional    ${statistics.mean([t['notional'] for t in all_trades]):,.2f}")
        print(f"  trades/session  {len(all_trades) / sessions:.2f}" if sessions else "")
    else:
        print("  NO TRADES TAKEN")
        print(f"  signals found   {signals}")
        print(f"  rejected for whole-share sizing: {ws_skips}")
    return {"trades": len(all_trades), "net": net, "signals": signals,
            "whole_share_skips": ws_skips, "max_drawdown": maxdd}


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    equity = 290.0
    if "--equity" in sys.argv:
        equity = float(sys.argv[sys.argv.index("--equity") + 1])

    symbols = args or ([p.name for p in sorted(DATA_ROOT.iterdir()) if p.is_dir()]
                       if DATA_ROOT.exists() else [])
    if not symbols:
        print(f"No cached data found under {DATA_ROOT}.")
        print("Run examples/fetch_etf_intraday.py on a machine with TWS first.")
        sys.exit(1)

    loaded = {}
    for sym in symbols:
        got = load_symbol(sym)
        if got is None:
            print(f"  {sym}: no cached bars")
            continue
        loaded[sym] = got

    if not loaded:
        print("No usable cached data.")
        sys.exit(1)

    spans = [(t[0], t[-1], len(b)) for b, t in loaded.values()]
    print(f"data: {len(loaded)} symbols   "
          f"{min(s[0] for s in spans)[:10]} -> {max(s[1] for s in spans)[:10]}   "
          f"{min(s[2] for s in spans)}-{max(s[2] for s in spans)} bars each")

    full = [backtest_symbol(s, b, t, equity) for s, (b, t) in loaded.items()]
    report(full, equity, "FULL PERIOD (in-sample + out-of-sample)")

    # chronological 60/40 split, parameters never touched between them
    ins, oos = [], []
    for s, (b, t) in loaded.items():
        cut = int(len(b) * 0.60)
        ins.append(backtest_symbol(s, b, t, equity, 0, cut))
        oos.append(backtest_symbol(s, b, t, equity, cut))
    report(ins, equity, "IN-SAMPLE (first 60%)")
    report(oos, equity, "OUT-OF-SAMPLE (final 40%) — parameters unchanged")


if __name__ == "__main__":
    main()
