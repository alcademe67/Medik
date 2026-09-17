"""Frozen A/B/C momentum-entry research for the MEDIK ETF strategy.

RESEARCH ONLY — imports the live entry machinery so variant A is exactly the
live v2 rule; B and C change only the specified rules. Nothing here is wired to
the live bot. See the frozen methodology in the session notes.

    python backtest/momentum_research_bt.py --equity 638 [--symbols SPY QQQ IWM]

Variants (each a fixed hypothesis, run once — never tuned to force trades):
  A  Current v2:  score_candidate signal==TRADE (bullish + not-extended>1.5ATR)
                  AND qualifies_v2 (score>=85 + reclaim) AND net_edge; ATR stop,
                  fixed 1.5R target.
  B  Earlier entry: bullish 15m AND score>=SCORE_B AND (reclaim OR breakout)
                  AND rvol>=RVOL_B (developing, not surged) AND extension<=EXT_B
                  ATR (still refuses EXCESSIVE chase) AND net_edge; SAME fixed
                  1.5R exit as A, to isolate the entry change.
  C  B entry + dynamic exit: no fixed target; ATR chandelier trail + exit on
                  15m-trend flip or momentum_failed (closes below VWAP) + a
                  regime-to-cash filter (no entries when SPY 15m is not bullish).

Execution realism identical for all three (from backtest/medik_etf_bt.py): decide
on bar close, fill next open + half-spread + slippage, stop-first, real commissions.
In-sample = first 60% of sessions; out-of-sample = final 40%; rules frozen on IS.
"""
from __future__ import annotations

import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.medik_etf_bt import (
    SLIPPAGE_BPS, commission, half_spread, load_symbol, to_15m,
)
from strategy.medik_etf import (
    ETFSnapshot, PortfolioState, SizingRejected, score_candidate, size_trade,
    within_trading_window,
)
from strategy.medik_etf_v2 import net_edge_check

# ---- B/C frozen parameters (chosen once, on the objective, BEFORE seeing OOS)
SCORE_B = 70.0        # keep a real trend/quality bar, below v2's 85
RVOL_B = 1.0          # "developing" volume, not a 1.5x surge
EXT_B = 2.5           # still refuses EXCESSIVE extension (v2 uses 1.5)
# C exit
ATR_TRAIL = 2.5       # chandelier trail distance in ATRs
MOM_FAIL_BARS = 3     # consecutive 5m closes below VWAP -> momentum gone
TARGET_R = 1.5        # A/B fixed target in R


@dataclass
class Trade:
    symbol: str; entry_i: int; exit_i: int; entry: float; exit: float
    qty: int; net: float; gross: float; commission: float
    bars_held: int; reason: str; fav_excursion: float


def _snapshot(symbol, bars, times, i, bar_in_session):
    session_bars = bars[max(0, i - bar_in_session):i + 1]
    if len(session_bars) < 25:
        return None
    ts = times[i]
    bars15 = [b for b, t in _cache15(symbol, bars, times) if t <= ts]
    if len(bars15) < 25:
        return None
    price = bars[i].close
    hs = half_spread(symbol, price)
    return score_candidate(ETFSnapshot(
        symbol=symbol, price=price, bid=price - hs, ask=price + hs,
        bars_5m=session_bars, bars_15m=bars15,
        session_dollar_volume=sum(b.close * b.volume for b in session_bars)))


_C15: dict = {}
def _cache15(symbol, bars, times):
    if symbol not in _C15:
        b15, t15 = to_15m(bars, times)
        _C15[symbol] = list(zip(b15, t15))
    return _C15[symbol]


def bar_in_session(times, i):
    day = times[i][:10]; j = i; c = 0
    while j > 0 and times[j - 1][:10] == day:
        j -= 1; c += 1
    return c


def entry_gate(variant, cs, spy_bullish_at):
    """Return True if this candidate should be entered under the variant."""
    if cs is None:
        return False
    ext = (cs.price - cs.vwap) / cs.atr if cs.atr > 0 else 99.0
    if variant == "A":
        # faithful v2: score_candidate already requires bullish + ext<=1.5
        return cs.signal == "TRADE" and cs.score >= 85.0 and cs.pullback_reclaim
    # B and C share the entry
    if cs.trend_15m != "BULLISH":
        return False
    if cs.score < SCORE_B:
        return False
    if not (cs.pullback_reclaim or cs.breakout):
        return False
    if cs.rvol < RVOL_B:
        return False
    if ext > EXT_B:
        return False
    if variant == "C" and not spy_bullish_at:
        return False   # regime-to-cash: no new entries when the market is weak
    return True


def run_variant(symbol, bars, times, equity, variant, regime, start_idx, end_idx):
    trades: list[Trade] = []
    running = equity
    open_t = None          # dict with entry state
    session = None
    for i in range(start_idx, end_idx):
        day = times[i][:10]
        if day != session:
            session = day
        bis = bar_in_session(times, i)
        now_min = 9 * 60 + 30 + bis * 5
        nxt = bars[i + 1]

        # ---- manage an open position on the NEXT bar
        if open_t is not None:
            fill = open_t["fill"]; qty = open_t["qty"]
            open_t["hi"] = max(open_t["hi"], bars[i].high)
            exit_px = reason = None
            if variant in ("A", "B"):
                if nxt.open <= open_t["stop"]:
                    exit_px, reason = nxt.open, "stop_gap"
                elif nxt.low <= open_t["stop"]:
                    exit_px, reason = open_t["stop"], "stop"
                elif nxt.high >= open_t["target"]:
                    exit_px, reason = open_t["target"], "target"
                elif times[i + 1][:10] != day:
                    exit_px, reason = bars[i].close, "session_close"
            else:  # C dynamic
                cs = _snapshot(symbol, bars, times, i, bis)
                atr_now = cs.atr if cs else open_t["atr0"]
                trail = open_t["hi"] - ATR_TRAIL * atr_now
                eff_stop = max(open_t["stop"], trail)
                open_t["stop"] = eff_stop
                below = open_t["below"] + 1 if (cs and cs.price < cs.vwap) else 0
                open_t["below"] = below
                trend_ok = (cs is None) or (cs.trend_15m == "BULLISH")
                if nxt.open <= eff_stop:
                    exit_px, reason = nxt.open, "trail_gap"
                elif nxt.low <= eff_stop:
                    exit_px, reason = eff_stop, "trail_stop"
                elif not trend_ok:
                    exit_px, reason = bars[i].close, "trend_flip"
                elif below >= MOM_FAIL_BARS:
                    exit_px, reason = bars[i].close, "momentum_fail"
                elif times[i + 1][:10] != day:
                    exit_px, reason = bars[i].close, "session_close"

            if exit_px is not None:
                exit_px -= half_spread(symbol, exit_px)
                gross = (exit_px - fill) * qty
                ec = commission(qty, exit_px * qty)
                net = gross - open_t["entry_comm"] - ec
                running += net
                fav = open_t["hi"] - fill
                trades.append(Trade(symbol, open_t["entry_i"], i + 1, fill, exit_px, qty,
                                    net, gross, open_t["entry_comm"] + ec,
                                    (i + 1) - open_t["entry_i"], reason, fav))
                open_t = None
            continue

        # ---- entry search
        if not within_trading_window(now_min)[0]:
            continue
        cs = _snapshot(symbol, bars, times, i, bis)
        spy_ok = regime.get(times[i], True) if regime else True
        if not entry_gate(variant, cs, spy_ok):
            continue
        try:
            sized = size_trade(cs, PortfolioState(running, running, (), 0))
        except SizingRejected:
            continue
        fill = nxt.open * (1 + SLIPPAGE_BPS / 10_000.0) + half_spread(symbol, nxt.open)
        stop_d = sized.entry - sized.stop
        if stop_d <= 0:
            continue
        stop = fill - stop_d
        target = fill + stop_d * TARGET_R
        if variant in ("A", "B"):
            if not net_edge_check(symbol, sized.quantity, fill, stop, target).passes:
                continue
        else:
            # C has no fixed target; require the trail room to beat cost once
            probe = fill + stop_d * TARGET_R
            if not net_edge_check(symbol, sized.quantity, fill, stop, probe).passes:
                continue
        ec = commission(sized.quantity, fill * sized.quantity)
        open_t = dict(fill=fill, qty=sized.quantity, stop=stop, target=target,
                      entry_i=i + 1, entry_comm=ec, hi=nxt.open, below=0,
                      atr0=cs.atr)
    return trades


def build_regime(bars, times):
    """SPY 15m bullish-by-timestamp (EMA9>EMA21 on 15m), forward-filled to 5m."""
    b15, t15 = to_15m(bars, times)
    closes = [b.close for b in b15]
    def ema(vals, n):
        k = 2 / (n + 1); out = []; e = None
        for v in vals:
            e = v if e is None else v * k + e * (1 - k); out.append(e)
        return out
    e9, e21 = ema(closes, 9), ema(closes, 21)
    bull15 = {t15[i]: (e9[i] > e21[i]) for i in range(len(t15))}
    # forward-fill onto 5m timestamps
    regime = {}; last = True; gi = 0; keys = t15
    for t in times:
        while gi < len(keys) and keys[gi] <= t:
            last = bull15[keys[gi]]; gi += 1
        regime[t] = last
    return regime


# ------------------------------------------------------------------- metrics

def metrics(trades: list[Trade], equity, sessions):
    n = len(trades)
    if n == 0:
        return dict(trades=0, per_wk=0.0, win=0.0, pf=0.0, net=0.0, net_pct=0.0,
                    maxdd=0.0, avg=0.0, hold_min=0.0, capture=0.0)
    nets = [t.net for t in trades]
    wins = [t for t in trades if t.net > 0]; losses = [t for t in trades if t.net <= 0]
    gains = sum(t.net for t in wins); pains = -sum(t.net for t in losses)
    # pooled equity curve by exit order -> max drawdown
    ordered = sorted(trades, key=lambda t: t.exit_i)
    eq = equity; peak = equity; dd = 0.0
    for t in ordered:
        eq += t.net; peak = max(peak, eq); dd = max(dd, (peak - eq) / peak if peak > 0 else 0)
    caps = [max(0.0, min(1.0, (t.exit - t.entry) / t.fav_excursion))
            for t in trades if t.fav_excursion > 0]
    weeks = max(1.0, sessions / 5.0)
    return dict(
        trades=n, per_wk=n / weeks, win=len(wins) / n,
        pf=(gains / pains) if pains > 0 else float("inf"),
        net=sum(nets), net_pct=sum(nets) / equity * 100.0,
        maxdd=dd * 100.0, avg=statistics.mean(nets),
        hold_min=statistics.mean([t.bars_held * 5 for t in trades]),
        capture=statistics.mean(caps) if caps else 0.0)


def main():
    equity = 638.0; symbols = ["SPY", "QQQ", "IWM"]
    args = sys.argv[1:]
    while args:
        a = args.pop(0)
        if a == "--equity": equity = float(args.pop(0))
        elif a == "--symbols":
            symbols = []
            while args and not args[0].startswith("--"): symbols.append(args.pop(0))
    data = {s: load_symbol(s) for s in symbols}
    data = {s: v for s, v in data.items() if v}
    if not data:
        print("no data"); return
    # regime from SPY (fallback: first symbol)
    reg_src = "SPY" if "SPY" in data else next(iter(data))
    regime = build_regime(*data[reg_src])

    # session-based IS/OOS split per symbol
    def split_idx(times):
        days = sorted(set(t[:10] for t in times))
        cut_day = days[int(len(days) * 0.6)]
        cut = next(i for i, t in enumerate(times) if t[:10] >= cut_day)
        return cut, len(days)

    print(f"MOMENTUM ENTRY RESEARCH  equity=${equity:,.0f}  symbols={list(data)}")
    print("(pooled across symbols; per-symbol single position; IS=first 60% sessions)\n")
    for label, lo, hi in [("IN-SAMPLE", "is", None), ("OUT-OF-SAMPLE", "oos", None)]:
        pooled = {v: [] for v in ("A", "B", "C")}
        total_sessions = 0
        for s, (bars, times) in data.items():
            cut, ndays = split_idx(times)
            if label == "IN-SAMPLE":
                lo_i, hi_i = 0, cut
                total_sessions += int(ndays * 0.6)
            else:
                lo_i, hi_i = cut, len(bars) - 1
                total_sessions += ndays - int(ndays * 0.6)
            for v in ("A", "B", "C"):
                pooled[v].extend(run_variant(s, bars, times, equity, v, regime, lo_i, hi_i))
        sess = max(1, total_sessions // max(1, len(data)))
        print(f"===== {label} ({sess} sessions/symbol) =====")
        print(f"{'var':<4}{'trades':>7}{'trd/wk':>8}{'win':>7}{'PF':>7}"
              f"{'net$':>9}{'net%':>7}{'maxDD':>7}{'avg$':>8}{'holdmin':>8}{'capture':>8}")
        for v in ("A", "B", "C"):
            m = metrics(pooled[v], equity, sess)
            pf = "inf" if m["pf"] == float("inf") else f"{m['pf']:.2f}"
            print(f"{v:<4}{m['trades']:>7}{m['per_wk']:>8.2f}{m['win']:>7.0%}{pf:>7}"
                  f"{m['net']:>+9.2f}{m['net_pct']:>+7.1f}{m['maxdd']:>7.1f}"
                  f"{m['avg']:>+8.3f}{m['hold_min']:>8.0f}{m['capture']:>8.0%}")
        print()


if __name__ == "__main__":
    main()
