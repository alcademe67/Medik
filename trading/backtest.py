"""Event-driven backtester with real performance metrics.

Runs `strategy.generate_signals` bar-by-bar with an ATR stop-loss and a
risk/reward take-profit (the risk layer), charging a fee on every fill.
Long-only, one position at a time. Returns per-trade records, an equity
curve, and the metrics quants actually look at: CAGR, Sharpe, max
drawdown, win rate, profit factor.

Execution model (stated plainly so results aren't over-trusted):
  * signals are computed on a bar's close and acted on at that close;
  * stop/target are checked against each later bar's low/high;
  * if both could trigger in one bar we assume the stop hit first (the
    pessimistic, honest assumption).
Real fills are slightly worse than this — treat metrics as optimistic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from trading import strategy
from trading.config import DEFAULT_PARAMS, StrategyParams

# Bars per year for annualising, by timeframe.
_TF_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "8h": 480, "12h": 720,
    "1d": 1440, "1w": 10080,
}


def bars_per_year(timeframe: str) -> float:
    minutes = _TF_MINUTES.get(timeframe, 60)
    return (365 * 24 * 60) / minutes


@dataclass
class Trade:
    entry_time: object
    exit_time: object
    entry_price: float
    exit_price: float
    reason: str  # 'target' | 'stop' | 'signal' | 'end'
    return_pct: float


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    initial_cash: float = 0.0
    final_equity: float = 0.0
    timeframe: str = "1h"

    # metrics
    total_return_pct: float = 0.0
    cagr_pct: float = 0.0
    sharpe: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0

    @property
    def num_trades(self) -> int:
        return len(self.trades)


def _metrics(result: BacktestResult) -> BacktestResult:
    eq = result.equity
    result.final_equity = float(eq.iloc[-1]) if len(eq) else result.initial_cash
    result.total_return_pct = (result.final_equity / result.initial_cash - 1) * 100

    if len(eq) > 1:
        bar_returns = eq.pct_change().dropna()
        n_year = bars_per_year(result.timeframe)
        years = len(eq) / n_year
        if years > 0 and result.final_equity > 0:
            result.cagr_pct = ((result.final_equity / result.initial_cash) ** (1 / years) - 1) * 100
        std = bar_returns.std()
        if std and std > 0:
            result.sharpe = float(bar_returns.mean() / std * np.sqrt(n_year))
        running_max = eq.cummax()
        drawdown = eq / running_max - 1
        result.max_drawdown_pct = float(drawdown.min() * 100)

    wins = [t for t in result.trades if t.return_pct > 0]
    if result.trades:
        result.win_rate_pct = len(wins) / len(result.trades) * 100
    gross_profit = sum(t.return_pct for t in wins)
    gross_loss = -sum(t.return_pct for t in result.trades if t.return_pct <= 0)
    result.profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    return result


def run(
    df: pd.DataFrame,
    params: StrategyParams = DEFAULT_PARAMS,
    *,
    timeframe: str = "1h",
    initial_cash: float = 10_000.0,
    fee_rate: float = 0.001,
) -> BacktestResult:
    """Backtest the strategy on one OHLCV frame (all-in per position)."""
    data = strategy.generate_signals(df, params)

    cash = initial_cash
    position = 0.0          # base units held
    entry_price = stop = target = 0.0
    entry_time = None
    trades: list[Trade] = []
    equity_points: list[float] = []

    def close_trade(exit_price: float, when, reason: str) -> None:
        nonlocal cash, position, entry_price
        proceeds = position * exit_price * (1 - fee_rate)
        cash = proceeds
        ret = (exit_price - entry_price) / entry_price * 100
        trades.append(Trade(entry_time, when, entry_price, exit_price, reason, ret))
        position = 0.0

    for when, row in data.iterrows():
        price = row["close"]
        if position > 0:
            # Risk exits first, checked against this bar's range.
            if row["low"] <= stop:
                close_trade(stop, when, "stop")
            elif row["high"] >= target:
                close_trade(target, when, "target")
            elif bool(row["exit"]):
                close_trade(price, when, "signal")

        if position == 0.0 and bool(row["entry"]) and np.isfinite(row.get("atr", np.nan)):
            atr_val = row["atr"]
            if atr_val > 0:
                entry_price = price
                stop = price - params.atr_stop_mult * atr_val
                target = price + params.atr_stop_mult * params.risk_reward * atr_val
                entry_time = when
                position = (cash * (1 - fee_rate)) / price
                cash = 0.0

        equity_points.append(cash + position * price)

    # Mark any open position to the final close.
    if position > 0:
        close_trade(float(data["close"].iloc[-1]), data.index[-1], "end")
        equity_points[-1] = cash

    result = BacktestResult(
        trades=trades,
        equity=pd.Series(equity_points, index=data.index),
        initial_cash=initial_cash,
        timeframe=timeframe,
    )
    return _metrics(result)


def format_report(symbol: str, result: BacktestResult) -> str:
    pf = "∞" if result.profit_factor == float("inf") else f"{result.profit_factor:.2f}"
    return "\n".join(
        [
            f"Backtest — {symbol} @ {result.timeframe}  ({len(result.equity)} bars)",
            "-" * 46,
            f"Trades          : {result.num_trades}",
            f"Win rate        : {result.win_rate_pct:.1f}%",
            f"Total return    : {result.total_return_pct:+.2f}%",
            f"CAGR            : {result.cagr_pct:+.2f}%",
            f"Sharpe          : {result.sharpe:.2f}",
            f"Max drawdown    : {result.max_drawdown_pct:.2f}%",
            f"Profit factor   : {pf}",
            f"Final equity    : {result.final_equity:.2f} (from {result.initial_cash:.0f})",
            "-" * 46,
            "Research only — optimistic fills, past != future.",
        ]
    )
