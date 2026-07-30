"""`python -m bot.diag` — read-only diagnostic: why isn't the bot trading?

Prints the live gate, the candle/timeframe config, and — for each watchlist
symbol — how many candles KuCoin actually returns and the regime/signal the
bot computes from them. Places NO orders and touches nothing.

Read the output like this:
  * candles < the "need" number  -> KuCoin isn't returning enough history for
    this timeframe; the regime stays "warming-up" and the bot won't trade.
  * regime is bull/bear/sideways/high_vol and signal=HOLD -> it's warmed up and
    simply waiting for a setup (normal — not a bug).
  * Live gate CLOSED -> it's not armed for real orders.
"""

import asyncio

from bot import config, kucoin_client, preflight, regime_signal
from trading.config import DEFAULT_PARAMS


async def _run() -> None:
    need = regime_signal.min_bars(DEFAULT_PARAMS)
    print("\n=== Bot diagnostic (read-only — places no orders) ===\n")
    allowed, reason = preflight.live_allowed()
    print(f"Live gate      : {'OPEN' if allowed else 'CLOSED'} — {reason}")
    print(f"KLINE_TYPE     : {config.KLINE_TYPE}")
    print(f"REGIME_CANDLES : {config.REGIME_CANDLES}")
    print(f"Min candles    : {need} to leave warming-up (trend EMA {DEFAULT_PARAMS.ema_trend} keeps improving past that)")
    print(f"Watchlist      : {', '.join(config.TRADE_SYMBOLS)}\n")

    for sym in config.TRADE_SYMBOLS:
        try:
            rows = await kucoin_client.fetch_ohlcv(sym, config.KLINE_TYPE, limit=config.REGIME_CANDLES)
            df = regime_signal.frame_from_ohlcv(rows)
            sig, reg = regime_signal.signal_from_frame(df)
            flag = "  <-- TOO FEW CANDLES (warming-up)" if len(df) < need else ""
            print(f"  {sym:<12} candles={len(df):<5} regime={reg:<11} signal={sig.value:<5}{flag}")
        except Exception as exc:  # noqa: BLE001 - report any per-symbol failure
            print(f"  {sym:<12} ERROR: {exc}")

    print(
        "\nIf candles are below the 'need' number, KuCoin isn't returning enough history\n"
        "for this timeframe — tell Claude the numbers. If regime is a real state and\n"
        "signal=HOLD, the bot is warmed up and just waiting for a setup (normal).\n"
    )
    await kucoin_client.aclose()


if __name__ == "__main__":
    asyncio.run(_run())
