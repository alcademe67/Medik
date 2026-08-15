"""Backtest the ALCA multi-timeframe strategy on real IBKR bars.

Timing discipline (the thing that makes a backtest worth believing):

* A signal is computed from COMPLETED 15-minute bars only, and filled at the
  NEXT bar's open. Never at the signal bar's close.
* Weekly and daily context comes strictly from sessions BEFORE the signal
  bar's date, so a day's own outcome can't leak into the setup that entered
  it.
* When a bar's range covers both the stop and the target, the STOP is
  assumed to fill first. Intrabar sequence is unknowable from OHLC, and
  assuming the good one is how backtests flatter themselves.
* Every fill pays the account's real commission:
  clamp($0.005/share, min $1.00, max 1% of trade value).

Positions are also force-closed at the last bar of each session, because the
strategy is intraday — this is what makes commissions bite, and it is the
honest way to model it.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.alca_mtf import OHLCV, generate_signal

PER_SHARE = 0.005
MIN_COMMISSION = 1.00
MAX_PCT_OF_VALUE = 0.01


def commission(shares: float, trade_value: float) -> float:
    if shares <= 0 or trade_value <= 0:
        return 0.0
    return min(max(PER_SHARE * shares, MIN_COMMISSION), MAX_PCT_OF_VALUE * trade_value)


@dataclass
class Trade:
    symbol: str
    entry_date: str
    entry: float
    exit: float
    shares: float
    exit_reason: str
    gross: float
    commissions: float

    @property
    def net(self) -> float:
        return self.gross - self.commissions


@dataclass
class BacktestResult:
    trades: list = field(default_factory=list)
    signals_evaluated: int = 0
    signals_fired: int = 0

    @property
    def gross(self) -> float:
        return sum(t.gross for t in self.trades)

    @property
    def commissions(self) -> float:
        return sum(t.commissions for t in self.trades)

    @property
    def net(self) -> float:
        return self.gross - self.commissions

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.net > 0)

    def profit_factor(self, net: bool = True) -> float:
        gains = sum((t.net if net else t.gross) for t in self.trades
                    if (t.net if net else t.gross) > 0)
        losses = -sum((t.net if net else t.gross) for t in self.trades
                      if (t.net if net else t.gross) < 0)
        if losses <= 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses


def _session_of(ts: str) -> str:
    return ts[:10]


def run_backtest(
    symbol: str,
    bars_15m: list[OHLCV],
    times_15m: list[str],
    daily: list[OHLCV],
    daily_dates: list[str],
    weekly: list[OHLCV],
    weekly_dates: list[str],
    equity: float,
    risk_pct: float = 1.0,
    position_pct: float = 10.0,
    warmup: int = 40,
) -> BacktestResult:
    """One symbol, one pass. Long-only (the account cannot short)."""
    result = BacktestResult()
    avg_daily_volume = (
        sum(b.volume for b in daily[-60:]) / min(len(daily), 60) if daily else 0.0
    )

    position = None  # (entry_price, shares, stop, target, entry_ts, entry_comm)

    for i in range(warmup, len(bars_15m) - 1):
        ts = times_15m[i]
        session = _session_of(ts)
        next_bar = bars_15m[i + 1]
        last_of_session = _session_of(times_15m[i + 1]) != session

        # ---- manage an open position on the NEXT bar
        if position is not None:
            entry_px, shares, stop, target, entry_ts, entry_comm = position
            exit_px = exit_reason = None
            if next_bar.low <= stop:
                exit_px, exit_reason = stop, "stop"          # stop wins ties
            elif next_bar.high >= target:
                exit_px, exit_reason = target, "target"
            elif last_of_session:
                exit_px, exit_reason = next_bar.close, "session_close"

            if exit_px is not None:
                gross = (exit_px - entry_px) * shares
                exit_comm = commission(shares, exit_px * shares)
                result.trades.append(Trade(
                    symbol=symbol, entry_date=entry_ts, entry=entry_px, exit=exit_px,
                    shares=shares, exit_reason=exit_reason, gross=gross,
                    commissions=entry_comm + exit_comm,
                ))
                position = None
            continue  # never hold two positions in one symbol

        # ---- look for an entry, using only history strictly before today
        d_hist = [b for b, d in zip(daily, daily_dates) if d < session]
        w_hist = [b for b, d in zip(weekly, weekly_dates) if d < session]
        if len(d_hist) < 60 or len(w_hist) < 40:
            continue

        window = bars_15m[max(0, i - 200):i + 1]     # completed bars only
        vol_ratio = (
            window[-1].volume / (sum(b.volume for b in window[-20:]) / 20)
            if len(window) >= 20 and sum(b.volume for b in window[-20:]) > 0 else 1.0
        )

        result.signals_evaluated += 1
        sig = generate_signal(
            symbol=symbol, weekly=w_hist, daily=d_hist, entry=window,
            avg_daily_volume=avg_daily_volume, volume_ratio=vol_ratio,
            account_can_short=False,
        )
        if sig.action != "BUY" or sig.stop_loss is None:
            continue

        fill = next_bar.open                          # fill at the NEXT open
        risk_per_share = fill - sig.stop_loss
        if risk_per_share <= 0:
            continue
        shares = min(
            equity * risk_pct / 100.0 / risk_per_share,
            equity * position_pct / 100.0 / fill,
        )
        if shares <= 0:
            continue

        result.signals_fired += 1
        position = (fill, shares, sig.stop_loss, sig.take_profit, ts,
                    commission(shares, fill * shares))

    return result
