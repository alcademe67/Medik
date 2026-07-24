"""The trading engine: scan → signal → risk-check → execute, on a loop.

Safe to start at any time. It only places REAL orders when the gate in
`preflight.live_allowed()` is satisfied (LIVE_TRADING=true AND the
operator ran `bot.golive`); otherwise it runs in paper mode with
simulated fills. Every action is logged with a timestamp and announced to
Telegram. Open positions live in SQLite, so a restart/crash resumes
cleanly.

    python -m bot.live_engine
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from bot import config, execution, kucoin_client, notify, preflight, risk, state
from bot.strategy import Signal, crossover_signal

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("live_engine")

PAPER_EQUITY = float(os.getenv("PAPER_EQUITY", "1000"))


def _atr(candles: list[tuple[float, float, float]], period: int = 14) -> float:
    """Wilder ATR from (high, low, close) candles, oldest to newest."""
    trs, prev_close = [], None
    for high, low, close in candles:
        tr = high - low if prev_close is None else max(
            high - low, abs(high - prev_close), abs(low - prev_close)
        )
        trs.append(tr)
        prev_close = close
    if len(trs) < period:
        return 0.0
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


class Engine:
    def __init__(self):
        self.symbol = config.TRADE_SYMBOL
        self.live = False
        self._stop = asyncio.Event()

    async def _equity(self) -> float:
        """Account equity used for position sizing (quote currency)."""
        try:
            bal = await kucoin_client.get_available_balance(config.QUOTE_CURRENCY)
        except kucoin_client.KucoinError:
            bal = 0.0
        if not self.live and bal <= 0:
            return PAPER_EQUITY  # let paper mode work on an empty account
        return bal

    async def _manage_open(self, price: float, exit_signal: bool) -> None:
        for pos in state.open_positions():
            if pos.symbol != self.symbol:
                continue
            reason = None
            if price <= pos.stop:
                reason = "stop"
            elif price >= pos.target:
                reason = "target"
            elif exit_signal:
                reason = "signal"
            if reason:
                pnl, res = await execution.close_long(pos, price, reason)
                tag = "" if res.get("dryRun") is False else "PAPER "
                emoji = "🎯" if reason == "target" else "🛑" if reason == "stop" else "↩️"
                await notify.send(
                    f"{emoji} {tag}SELL {pos.symbol} @ {price:.6f} ({reason}) "
                    f"P/L {pnl:+.4f} {config.QUOTE_CURRENCY}"
                )

    async def _maybe_open(self, price: float, atr: float) -> None:
        if atr <= 0 or state.has_open_symbol(self.symbol):
            return
        decision = risk.check_can_trade(
            open_positions=state.count_open(),
            realized_pnl_today=state.today_realized_pnl(),
            start_equity_today=state.today_start_equity(),
            consecutive_losses=state.consecutive_losses(),
        )
        if not decision.allowed:
            logger.info("entry skipped: %s", decision.reason)
            return

        equity = await self._equity()
        stop, target = risk.stop_and_target(price, atr)
        size = risk.position_size(
            equity=equity, entry_price=price, stop_price=stop, available_quote=equity
        )
        if size <= 0:
            return
        try:
            _id, res = await execution.open_long(self.symbol, size, price, stop, target, live=self.live)
        except execution.ValidationError as exc:
            logger.info("entry rejected by validation: %s", exc)
            return
        tag = "" if res.get("dryRun") is False else "PAPER "
        await notify.send(
            f"🟢 {tag}BUY {self.symbol} @ {price:.6f} size {size} "
            f"(stop {stop:.6f}, target {target:.6f})"
        )

    async def _tick(self) -> None:
        candles = await kucoin_client.fetch_candles(self.symbol, config.KLINE_TYPE, limit=200)
        closes = [c[2] for c in candles]
        if len(closes) < config.SLOW_MA + 1:
            return
        price = closes[-1]
        signal = crossover_signal(closes, config.FAST_MA, config.SLOW_MA)
        await state.ensure_day(await self._equity())
        await self._manage_open(price, exit_signal=(signal == Signal.SELL))
        if signal == Signal.BUY:
            await self._maybe_open(price, _atr(candles))

    async def run(self) -> None:
        state.init_db()
        self.live, reason = preflight.live_allowed()

        checks = await preflight.run_health_checks()
        for c in checks:
            logger.info("healthcheck [%s] %s — %s", "PASS" if c.passed else "FAIL", c.name, c.detail)
        if self.live and not preflight.all_passed(checks):
            self.live = False
            reason = "health checks failed — forced to paper mode"
            logger.warning(reason)

        recovered = [p for p in state.open_positions() if p.symbol == self.symbol]
        mode = "LIVE 💸" if self.live else "PAPER (simulated)"
        await notify.send(
            f"🤖 Engine started — {mode} — {self.symbol}, MA({config.FAST_MA}/{config.SLOW_MA}). "
            f"{reason}. Recovered {len(recovered)} open position(s)."
        )
        logger.info("engine start: mode=%s reason=%s recovered=%d", mode, reason, len(recovered))

        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except kucoin_client.KucoinError as exc:
                    logger.warning("tick error: %s", exc)
                    await notify.send(f"⚠️ Engine tick error: {exc}")
                except Exception as exc:  # noqa: BLE001 - keep the loop alive, report it
                    logger.exception("unexpected tick error")
                    await notify.send(f"⚠️ Engine error: {exc}")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=config.TRADE_INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    pass
        finally:
            await notify.send("🛑 Engine shutting down.")
            await kucoin_client.aclose()

    def stop(self) -> None:
        self._stop.set()


async def _main() -> None:
    engine = Engine()
    try:
        await engine.run()
    except KeyboardInterrupt:
        engine.stop()


if __name__ == "__main__":
    asyncio.run(_main())
