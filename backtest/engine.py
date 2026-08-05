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
from strategy.pullback import (
    PULLBACK_LOOKBACK,
    TREND_SMA,
    compute_pullback_frame,
    evaluate_pullback,
)
from strategy.risk import RiskRejected, size_position
from strategy.risk_limits import AccountState, check_trade_allowed
from strategy.scoring import score_row
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
    quantity: float = 0
    stop: float = 0.0
    target: float = 0.0
    capital_at_risk: float = 0.0
    realized_pnl: float = 0.0
    r_multiple: float = 0.0
    strategy: str = "gate"


def _precompute_pullback_signals(price_data: dict, config: StrategyConfig) -> dict:
    """{symbol: {date: (entry, stop, target)}} for every bar where the
    pullback strategy fires.

    Each bar is evaluated against a slice ending at that bar, so no
    evaluation can see a future bar -- the same no-lookahead guarantee the
    rest of this engine makes. Precomputed per-symbol here because the main
    loop iterates a date-merged frame across all symbols and no longer has
    each symbol's own history at hand.
    """
    out: dict = {}
    first_valid = TREND_SMA + PULLBACK_LOOKBACK + 1
    for symbol, df in price_data.items():
        if len(df) <= first_valid:
            continue
        frame = compute_pullback_frame(df)
        fired: dict = {}
        for i in range(first_valid, len(frame)):
            sig = evaluate_pullback(frame.iloc[: i + 1], config)
            if sig.passed:
                fired[frame.index[i]] = (sig.entry, sig.stop, sig.target)
        if fired:
            out[symbol] = fired
    return out


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
    fractional: bool = False,
    enforce_score_threshold: bool = False,
    enforce_risk_limits: bool = False,
    signal_source: str = "gate",
    long_only: bool = False,
) -> BacktestResult:
    """price_data: {symbol: DataFrame} with columns open/high/low/close/volume,
    indexed by date ascending. Each frame needs config.warmup_bars rows before
    any signal can fire.

    fractional, enforce_score_threshold, and enforce_risk_limits all default
    to False, which reproduces the original backtest behavior exactly
    (whole-share sizing off the notional cap only, no score gate, no
    portfolio circuit breakers) -- that's the version already validated
    against real IBKR data. Set all three True to backtest the system as it
    actually runs live/paper today: fractional risk-capped sizing, the
    scoring engine's threshold, and the daily/weekly/monthly drawdown +
    max-concurrent-positions circuit breakers from strategy.risk_limits.

    signal_source selects which strategy generates entries:
      "gate"     -- the 4-indicator gate only (default, original behavior)
      "pullback" -- the trend-pullback strategy only
      "both"     -- gate first, pullback as fallback, matching how
                    service/pipeline.py picks a signal live.

    long_only=True discards SELL/short signals, matching the owner's
    long-only TFSA. The default False keeps the original both-sides
    behavior so existing gate results stay reproducible.

    Pullback entries carry ABSOLUTE stop and target levels (structure: the
    pullback low and the swing high). Those levels do not shift with the
    fill price the way the gate's ATR-derived stop does -- a support level
    is where it is regardless of where the next bar opens. If the fill gaps
    far enough that the swing-high target no longer clears the strategy's
    minimum R:R, size_position rejects the trade, which is the honest
    outcome rather than silently taking a worse trade than the one signalled.
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

    pullback_signals: dict = {}
    if signal_source in ("pullback", "both"):
        pullback_signals = _precompute_pullback_signals(price_data, config)

    merged = pd.concat(frames.values()).sort_index(kind="stable")

    cash = starting_capital
    open_positions: dict[str, Trade] = {}
    pending_entries: dict[str, dict] = {}
    trades: list[Trade] = []
    equity_curve: list = []

    # equity = cash + mark-to-market value of open positions, recomputed per date
    last_close: dict[str, float] = {}

    # Baselines for the daily/weekly/monthly circuit breakers, only used
    # when enforce_risk_limits=True. Reset to the prior day's closing
    # equity whenever a new day/ISO-week/calendar-month begins.
    day_baseline = week_baseline = month_baseline = starting_capital
    tracked_week: tuple | None = None
    tracked_month: tuple | None = None

    def _mark_to_market(positions: dict[str, Trade]) -> float:
        return sum(
            p.quantity * last_close.get(s, p.entry_price)
            if p.side == "BUY"
            else p.quantity * (2 * p.entry_price - last_close.get(s, p.entry_price))
            for s, p in positions.items()
        )

    for date, group in merged.groupby(merged.index):
        equity_before_today = cash + _mark_to_market(open_positions)
        day_baseline = equity_before_today
        iso_year, iso_week, _ = date.isocalendar()
        if (iso_year, iso_week) != tracked_week:
            week_baseline = equity_before_today
            tracked_week = (iso_year, iso_week)
        if (date.year, date.month) != tracked_month:
            month_baseline = equity_before_today
            tracked_month = (date.year, date.month)

        for _, row in group.iterrows():
            symbol = row["symbol"]
            last_close[symbol] = row["close"]

            # 1) Fill any pending entry scheduled from the previous bar, at this bar's open.
            pending = pending_entries.pop(symbol, None)
            if pending is not None and symbol not in open_positions:
                side = pending["side"]
                fill_price = float(row["open"])
                strategy_used = pending.get("strategy", "gate")

                if strategy_used == "pullback":
                    # Structural levels are absolute -- support and
                    # resistance sit where they sit, regardless of where
                    # the next bar happens to open.
                    stop = pending["stop"]
                    explicit_target = pending["target"]
                    entry_min_rr = pending["min_rr"]
                else:
                    stop_distance = abs(pending["entry"] - pending["stop"])
                    stop = fill_price - stop_distance if side == "BUY" else fill_price + stop_distance
                    explicit_target = None
                    entry_min_rr = None

                open_val = _mark_to_market(open_positions)
                equity = cash + open_val

                try:
                    plan = size_position(
                        side, fill_price, stop, cash, config,
                        net_liquidation=equity if (fractional or enforce_risk_limits) else None,
                        fractional=fractional,
                        target=explicit_target,
                        min_rr=entry_min_rr,
                    )
                except (RiskRejected, ValueError):
                    # ValueError covers a gap through the structural stop,
                    # which makes the signalled trade invalid rather than
                    # merely unattractive.
                    plan = None

                if plan is not None:
                    if enforce_risk_limits:
                        state = AccountState(
                            net_liquidation=equity,
                            gross_position_value=open_val,
                            open_position_count=len(open_positions),
                            equity_start_of_day=day_baseline,
                            equity_start_of_week=week_baseline,
                            equity_start_of_month=month_baseline,
                        )
                        allowed, _reason = check_trade_allowed(state, config)
                        if not allowed:
                            plan = None
                    else:
                        # portfolio-level cap only: total invested must stay under max_deployed_pct of equity
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
                            strategy=strategy_used,
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
                fired = False
                if signal_source in ("gate", "both"):
                    sig = evaluate_row(row, config)
                    if sig.passed and not (long_only and sig.side != "BUY"):
                        take = True
                        if enforce_score_threshold:
                            score = score_row(row, sig, config)
                            take = score.tradeable
                        if take:
                            pending_entries[symbol] = {
                                "side": sig.side, "entry": sig.entry, "stop": sig.stop,
                                "target": None, "min_rr": None, "strategy": "gate",
                            }
                            fired = True
                if not fired and signal_source in ("pullback", "both"):
                    hit = pullback_signals.get(symbol, {}).get(date)
                    if hit is not None:
                        p_entry, p_stop, p_target = hit
                        pending_entries[symbol] = {
                            "side": "BUY", "entry": p_entry, "stop": p_stop,
                            "target": p_target, "min_rr": config.pullback_min_rr,
                            "strategy": "pullback",
                        }

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
