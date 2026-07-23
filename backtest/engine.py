"""Multi-symbol, no-lookahead backtester for the strategy in strategy/signals.py.

Timing model: a signal computed from bar t's close is filled at bar t+1's
open (never the same bar it was decided on). Exits are checked against each
bar's high/low; if both the stop and target fall inside the same bar, the
stop is assumed to hit first (conservative). All symbols share one cash
pool, so position sizing reacts to whatever capital is actually free at
the time -- two symbols can't each claim 20% of the same starting balance
simultaneously.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from strategy.config import StrategyConfig, DEFAULT_CONFIG
from strategy.risk import RiskRejected, size_position
from strategy.signals import compute_indicator_frame, evaluate_row


@dataclass
class Trade:
    symbol: str
    side: str
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: pd.Timestamp | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    quantity: int = 0
    stop: float = 0.0
    target: float = 0.0
    capital_at_risk: float = 0.0
    realized_pnl: float = 0.0
    r_multiple: float = 0.0


@dataclass
class BacktestResult:
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)  # list of (date, equity)
    starting_capital: float = 0.0
    ending_capital: float = 0.0

    @property
    def closed_trades(self):
        return [t for t in self.trades if t.exit_date is not None]

    def summary(self) -> dict:
        closed = self.closed_trades
        n = len(closed)
        if n == 0:
            return {"trades": 0}

        wins = [t for t in closed if t.realized_pnl > 0]
        losses = [t for t in closed if t.realized_pnl <= 0]
        gross_profit = sum(t.realized_pnl for t in wins)
        gross_loss = -sum(t.realized_pnl for t in losses)

        equity_values = [e for _, e in self.equity_curve]
        peak = float("-inf")
        max_dd = 0.0
        for v in equity_values:
            peak = max(peak, v)
            if peak > 0:
                max_dd = max(max_dd, (peak - v) / peak)

        total_return = (self.ending_capital - self.starting_capital) / self.starting_capital

        return {
            "trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / n,
            "avg_r_multiple": sum(t.r_multiple for t in closed) / n,
            "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else float("inf"),
            "total_return_pct": total_return * 100,
            "max_drawdown_pct": max_dd * 100,
            "starting_capital": self.starting_capital,
            "ending_capital": self.ending_capital,
        }


def run_backtest(
    price_data: dict,
    starting_capital: float,
    config: StrategyConfig = DEFAULT_CONFIG,
) -> BacktestResult:
    """price_data: {symbol: DataFrame} with columns open/high/low/close/volume,
    indexed by date ascending. Each frame needs config.warmup_bars rows before
    any signal can fire.
    """
    frames = {}
    for symbol, df in price_data.items():
        if len(df) <= config.warmup_bars:
            continue
        indf = compute_indicator_frame(df, config)
        indf["symbol"] = symbol
        frames[symbol] = indf

    if not frames:
        raise ValueError("no symbol has enough history to clear the warm-up window")

    merged = pd.concat(frames.values()).sort_index(kind="stable")

    cash = starting_capital
    open_positions: dict[str, Trade] = {}
    pending_entries: dict[str, dict] = {}
    trades: list[Trade] = []
    equity_curve: list = []

    # equity = cash + mark-to-market value of open positions, recomputed per date
    last_close: dict[str, float] = {}

    for date, group in merged.groupby(merged.index):
        for _, row in group.iterrows():
            symbol = row["symbol"]
            last_close[symbol] = row["close"]

            # 1) Fill any pending entry scheduled from the previous bar, at this bar's open.
            pending = pending_entries.pop(symbol, None)
            if pending is not None and symbol not in open_positions:
                side = pending["side"]
                planned_entry = pending["entry"]
                planned_stop = pending["stop"]
                stop_distance = abs(planned_entry - planned_stop)
                fill_price = float(row["open"])
                stop = fill_price - stop_distance if side == "BUY" else fill_price + stop_distance
                try:
                    plan = size_position(side, fill_price, stop, cash, config)
                except RiskRejected:
                    plan = None
                if plan is not None:
                    # portfolio-level cap: total invested must stay under max_deployed_pct of equity
                    open_val = sum(
                        p.quantity * last_close.get(s, p.entry_price)
                        if p.side == "BUY"
                        else p.quantity * (2 * p.entry_price - last_close.get(s, p.entry_price))
                        for s, p in open_positions.items()
                    )
                    equity = cash + open_val
                    if open_val + plan.position_value > config.max_deployed_pct * equity:
                        plan = None
                if plan is not None:
                    cash -= plan.position_value
                    trades.append(
                        Trade(
                            symbol=symbol,
                            side=side,
                            entry_date=date,
                            entry_price=fill_price,
                            quantity=plan.quantity,
                            stop=plan.stop,
                            target=plan.target,
                            capital_at_risk=plan.capital_at_risk,
                        )
                    )
                    open_positions[symbol] = trades[-1]

            # 2) Check exits for any open position in this symbol against today's range.
            pos = open_positions.get(symbol)
            if pos is not None:
                hit_stop = row["low"] <= pos.stop if pos.side == "BUY" else row["high"] >= pos.stop
                hit_target = row["high"] >= pos.target if pos.side == "BUY" else row["low"] <= pos.target
                exit_price = None
                exit_reason = ""
                if hit_stop:
                    exit_price, exit_reason = pos.stop, "stop"
                elif hit_target:
                    exit_price, exit_reason = pos.target, "target"
                if exit_price is not None:
                    signed_qty = pos.quantity if pos.side == "BUY" else -pos.quantity
                    pnl = signed_qty * (exit_price - pos.entry_price)
                    cash += pos.quantity * exit_price if pos.side == "BUY" else pos.quantity * (2 * pos.entry_price - exit_price)
                    pos.exit_date = date
                    pos.exit_price = exit_price
                    pos.exit_reason = exit_reason
                    pos.realized_pnl = pnl
                    pos.r_multiple = pnl / pos.capital_at_risk if pos.capital_at_risk else 0.0
                    del open_positions[symbol]

            # 3) If flat and no fill pending, evaluate for a new signal on this bar's close.
            if symbol not in open_positions and symbol not in pending_entries:
                sig = evaluate_row(row, config)
                if sig.passed:
                    pending_entries[symbol] = {"side": sig.side, "entry": sig.entry, "stop": sig.stop}

        # mark-to-market equity at end of this date
        open_value = 0.0
        for sym, pos in open_positions.items():
            price = last_close.get(sym, pos.entry_price)
            open_value += pos.quantity * price if pos.side == "BUY" else pos.quantity * (2 * pos.entry_price - price)
        equity_curve.append((date, cash + open_value))

    # close anything still open at the last known price, marked distinctly from real exits
    for symbol, pos in list(open_positions.items()):
        price = last_close.get(symbol, pos.entry_price)
        signed_qty = pos.quantity if pos.side == "BUY" else -pos.quantity
        pnl = signed_qty * (price - pos.entry_price)
        cash += pos.quantity * price if pos.side == "BUY" else pos.quantity * (2 * pos.entry_price - price)
        pos.exit_date = merged.index.max()
        pos.exit_price = price
        pos.exit_reason = "backtest_end"
        pos.realized_pnl = pnl
        pos.r_multiple = pnl / pos.capital_at_risk if pos.capital_at_risk else 0.0

    result = BacktestResult(
        trades=trades,
        equity_curve=equity_curve,
        starting_capital=starting_capital,
        ending_capital=equity_curve[-1][1] if equity_curve else starting_capital,
    )
    return result
