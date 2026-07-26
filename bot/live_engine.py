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
from bot.logging_setup import setup_logging
from bot.strategy import Signal, crossover_signal

setup_logging()
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
        self.symbols = config.TRADE_SYMBOLS
        self.live = False
        self._stop = asyncio.Event()
        self._summary_day: str | None = None  # for the daily-summary rollover

    async def _equity(self) -> float:
        """Account equity used for position sizing (quote currency)."""
        try:
            bal = await kucoin_client.get_available_balance(config.QUOTE_CURRENCY)
        except kucoin_client.KucoinError:
            bal = 0.0
        if not self.live and bal <= 0:
            return PAPER_EQUITY  # let paper mode work on an empty account
        return bal

    async def _manage_open(self, symbol: str, price: float, atr: float, exit_signal: bool) -> None:
        for pos in state.open_positions():
            if pos.symbol != symbol:
                continue

            # Trail / breakeven the stop first (it can only move up).
            new_stop, high_water = risk.update_stop(
                entry_price=pos.entry_price, current_stop=pos.stop,
                high_water=pos.high_water, price=price, atr=atr,
            )
            if new_stop > pos.stop or high_water > pos.high_water:
                state.update_stop(pos.id, new_stop, high_water)
                if new_stop > pos.stop:
                    logger.info("stop moved up on %s: %.6f -> %.6f", pos.symbol, pos.stop, new_stop)
                    await notify.send(
                        f"🔒 Stop raised on {pos.symbol}: {pos.stop:.6f} → {new_stop:.6f}"
                    )
                pos.stop = new_stop  # use the tightened stop for the exit check below

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

    async def _maybe_open(self, symbol: str, price: float, atr: float) -> None:
        if atr <= 0 or state.has_open_symbol(symbol):
            return
        # Risk caps are GLOBAL — counted across every coin in the watchlist —
        # so more symbols never means more concurrent risk than configured.
        decision = risk.check_can_trade(
            open_positions=state.count_open(),
            realized_pnl_today=state.today_realized_pnl(),
            start_equity_today=state.today_start_equity(),
            consecutive_losses=state.consecutive_losses(),
            orders_today=state.orders_today(),
        )
        if not decision.allowed:
            logger.info("entry skipped (%s): %s", symbol, decision.reason)
            return

        equity = await self._equity()
        stop, target = risk.stop_and_target(price, atr)
        size = risk.position_size(
            equity=equity, entry_price=price, stop_price=stop, available_quote=equity
        )
        if size <= 0:
            return
        try:
            _id, res = await execution.open_long(symbol, size, price, stop, target, live=self.live)
        except execution.ValidationError as exc:
            logger.info("entry rejected by validation (%s): %s", symbol, exc)
            return
        tag = "" if res.get("dryRun") is False else "PAPER "
        await notify.send(
            f"🟢 {tag}BUY {symbol} @ {price:.6f} size {size} "
            f"(stop {stop:.6f}, target {target:.6f})"
        )

    async def _announce_emergency(self) -> None:
        msg = f"🚨 Emergency stop file '{config.EMERGENCY_STOP_PATH}' detected — halting."
        logger.critical(msg)
        await notify.send(msg)

    async def _maybe_daily_summary(self) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if self._summary_day is None:
            self._summary_day = today
        elif today != self._summary_day:
            pnl = state.realized_pnl_for_day(self._summary_day)
            st = state.stats()
            await notify.send(
                f"📊 Daily summary {self._summary_day}: realized P/L "
                f"{pnl:+.4f} {config.QUOTE_CURRENCY}. All-time: {st['total_trades']} "
                f"trades, {st['win_rate']:.1f}% win, total {st['total_pnl']:+.4f}."
            )
            self._summary_day = today

    async def _tick(self) -> None:
        # Emergency brake: a STOP file halts trading immediately.
        if os.path.exists(config.EMERGENCY_STOP_PATH):
            await self._announce_emergency()
            self._stop.set()
            return
        # Day bookkeeping + the daily summary are global — run them once a tick.
        state.ensure_day(await self._equity())
        await self._maybe_daily_summary()
        # Then evaluate each coin in the watchlist. A data error on one coin is
        # isolated here so it can't rob the other coins of their turn this tick.
        for symbol in self.symbols:
            try:
                await self._tick_symbol(symbol)
            except kucoin_client.KucoinError as exc:
                logger.warning("tick error on %s: %s", symbol, exc)

    async def _tick_symbol(self, symbol: str) -> None:
        """Fetch data for one coin, then manage its position and maybe enter."""
        candles = await kucoin_client.fetch_candles(symbol, config.KLINE_TYPE, limit=200)
        closes = [c[2] for c in candles]
        if len(closes) < config.SLOW_MA + 1:
            return
        price = closes[-1]
        atr = _atr(candles)
        signal = crossover_signal(closes, config.FAST_MA, config.SLOW_MA)
        await self._manage_open(symbol, price, atr, exit_signal=(signal == Signal.SELL))
        if signal == Signal.BUY:
            await self._maybe_open(symbol, price, atr)

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

        recovered = state.open_positions()
        mode = "LIVE 💸" if self.live else "PAPER (simulated)"
        watchlist = ", ".join(self.symbols)
        await notify.send(
            f"🤖 Engine started — {mode} — {len(self.symbols)} coin(s): {watchlist}. "
            f"MA({config.FAST_MA}/{config.SLOW_MA}). {reason}. "
            f"Recovered {len(recovered)} open position(s)."
        )
        logger.info(
            "engine start: mode=%s symbols=[%s] reason=%s recovered=%d",
            mode, watchlist, reason, len(recovered),
        )

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
