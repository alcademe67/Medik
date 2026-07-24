"""Backtest the trading strategy on historical KuCoin candles.

Replays past prices through the SAME signal function the live bot uses
(bot.strategy.crossover_signal), simulating buys and sells so you can see
how the strategy WOULD have performed before risking real money.

Run it (no API keys needed — candles are public):

    python -m bot.backtest                 # uses TRADE_SYMBOL / KLINE_TYPE from .env
    python -m bot.backtest ETH-USDT 15min

The single most important line in the output is the comparison to
"buy & hold": if the strategy earns less than simply buying and holding
the coin, the strategy is not worth running — most simple ones aren't.
Backtests are also optimistic (they assume you fill exactly at each
candle's close and that the past resembles the future — it doesn't), so
treat a good result as "worth trying on paper", never as a promise.
"""

import asyncio
import sys
from dataclasses import dataclass, field

from bot import config, kucoin_client
from bot.strategy import Signal, crossover_signal

# KuCoin spot taker fee (0.1%). Every simulated trade pays it, both ways.
DEFAULT_FEE = 0.001
# Candles returned per request by KuCoin's public endpoint.
MAX_CANDLES = 1500


@dataclass
class Trade:
    entry_price: float
    exit_price: float
    return_pct: float


@dataclass
class BacktestResult:
    candles: int
    trades: list[Trade] = field(default_factory=list)
    strategy_return_pct: float = 0.0
    buy_hold_return_pct: float = 0.0
    win_rate_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    final_equity: float = 0.0
    start_cash: float = 0.0

    @property
    def num_trades(self) -> int:
        return len(self.trades)

    @property
    def edge_pct(self) -> float:
        """How much the strategy beat (or lagged) buy & hold, in points."""
        return self.strategy_return_pct - self.buy_hold_return_pct


def simulate(
    closes: list[float],
    fast_period: int,
    slow_period: int,
    *,
    fee: float = DEFAULT_FEE,
    start_cash: float = 1000.0,
) -> BacktestResult:
    """Run the strategy over `closes` (oldest-to-newest) and score it.

    Uses an all-in / all-out model: each BUY puts the whole balance into
    the coin, each SELL returns it to cash. That isolates the strategy's
    quality and makes the buy-&-hold comparison fair (the live bot sizes
    trades far smaller, so real P&L scales down, but the return % and the
    edge over buy-&-hold are what tell you if the strategy is any good).
    """
    if len(closes) < slow_period + 1:
        return BacktestResult(candles=len(closes), start_cash=start_cash,
                              final_equity=start_cash)

    cash = start_cash
    position = 0.0  # base currency held
    entry_price = 0.0
    trades: list[Trade] = []
    peak = start_cash
    max_dd = 0.0
    last_signal = Signal.HOLD

    for i in range(len(closes)):
        price = closes[i]
        signal = crossover_signal(closes[: i + 1], fast_period, slow_period)

        # Mirror the live trader exactly: act only on a fresh, non-HOLD signal.
        if signal != Signal.HOLD and signal != last_signal:
            last_signal = signal
            if signal == Signal.BUY and position == 0.0 and cash > 0:
                position = cash * (1 - fee) / price
                entry_price = price
                cash = 0.0
            elif signal == Signal.SELL and position > 0.0:
                cash = position * price * (1 - fee)
                trades.append(
                    Trade(entry_price, price, (price - entry_price) / entry_price * 100)
                )
                position = 0.0

        equity = cash + position * price
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak > 0 else 0.0)

    final_equity = cash + position * closes[-1]
    wins = sum(1 for t in trades if t.return_pct > 0)
    return BacktestResult(
        candles=len(closes),
        trades=trades,
        strategy_return_pct=(final_equity / start_cash - 1) * 100,
        buy_hold_return_pct=(closes[-1] / closes[0] - 1) * 100,
        win_rate_pct=(wins / len(trades) * 100) if trades else 0.0,
        max_drawdown_pct=max_dd * 100,
        final_equity=final_equity,
        start_cash=start_cash,
    )


def format_report(symbol: str, kline_type: str, result: BacktestResult) -> str:
    if result.candles < 2:
        return f"Not enough candle data for {symbol} to backtest."

    if result.edge_pct >= 0:
        verdict = f"strategy BEAT buy & hold by {result.edge_pct:+.2f} points"
    else:
        verdict = f"strategy LAGGED buy & hold by {result.edge_pct:.2f} points"

    return "\n".join(
        [
            f"Backtest — {symbol} @ {kline_type}  ({result.candles} candles)",
            f"Strategy: MA crossover ({config.FAST_MA}/{config.SLOW_MA}), "
            f"fee {DEFAULT_FEE * 100:.2f}% per trade",
            "-" * 44,
            f"Round-trip trades : {result.num_trades}",
            f"Win rate          : {result.win_rate_pct:.1f}%",
            f"Strategy return   : {result.strategy_return_pct:+.2f}%",
            f"Buy & hold return : {result.buy_hold_return_pct:+.2f}%",
            f"Max drawdown      : {result.max_drawdown_pct:.2f}%",
            f"Final equity      : {result.final_equity:.2f} "
            f"(from {result.start_cash:.0f})",
            "-" * 44,
            f"Verdict: {verdict}.",
            "Past performance does not predict future results.",
        ]
    )


async def run(symbol: str, kline_type: str) -> BacktestResult:
    """Fetch historical candles and backtest the strategy on them."""
    closes = await kucoin_client.fetch_klines(symbol, kline_type, limit=MAX_CANDLES)
    return simulate(closes, config.FAST_MA, config.SLOW_MA)


async def _main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else config.TRADE_SYMBOL
    kline_type = sys.argv[2] if len(sys.argv) > 2 else config.KLINE_TYPE
    try:
        result = await run(symbol, kline_type)
    except kucoin_client.KucoinError as exc:
        raise SystemExit(f"Could not run backtest: {exc}")
    finally:
        await kucoin_client.aclose()
    print(format_report(symbol, kline_type, result))


if __name__ == "__main__":
    asyncio.run(_main())
