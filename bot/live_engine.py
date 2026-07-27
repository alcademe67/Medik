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

from bot import config, execution, kucoin_client, notify, preflight, regime_signal, risk, state
from bot.logging_setup import setup_logging
from bot.strategy import Signal

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

    async def _reconcile_positions(self) -> None:
        """Startup reconciliation: drop 'phantom' positions the exchange doesn't
        actually hold.

        A position recovered from SQLite may not correspond to a real holding —
        e.g. the buy never truly filled, or the coin was sold/moved outside the
        bot. Managing such a phantom would fire a SELL for base the account
        doesn't own → KuCoin 200004. So for each recovered position (live only —
        paper positions are simulated), compare its base-asset size to the
        available balance on the exchange and remove any that isn't backed.
        """
        if not self.live:
            return
        for pos in state.open_positions():
            base = pos.symbol.split("-")[0]
            try:
                held = await kucoin_client.get_available_balance(base, "trade")
            except kucoin_client.KucoinError as exc:
                logger.warning(
                    "reconcile %s: could not read %s balance (%s) — keeping position for now",
                    pos.symbol, base, exc,
                )
                continue
            if held < pos.size * 0.999:  # exchange doesn't hold enough base to back it
                state.remove_position(pos.id)
                msg = (
                    f"🧹 Removed phantom {pos.symbol} position (id={pos.id}): local size "
                    f"{pos.size:.8f} {base} but exchange holds only {held:.8f} {base}. "
                    f"No SELL sent."
                )
                logger.warning(msg)
                await notify.send(msg)

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
                if res.get("phantom") or res.get("aborted"):
                    continue  # no real SELL happened — nothing to announce
                tag = "" if res.get("dryRun") is False else "PAPER "
                emoji = "🎯" if reason == "target" else "🛑" if reason == "stop" else "↩️"
                await notify.send(
                    f"{emoji} {tag}SELL {pos.symbol} @ {price:.6f} ({reason}) "
                    f"P/L {pnl:+.4f} {config.QUOTE_CURRENCY}"
                )

    async def _maybe_open(self, symbol: str, price: float, atr: float) -> None:
        # [Q8] guard exits — logged so a skipped entry is never silent.
        if atr <= 0:
            logger.warning(
                "PIPE %s: NO ENTRY — ATR=%.8f <= 0, can't set a stop (need more/none-flat candles)",
                symbol, atr,
            )
            return
        if state.has_open_symbol(symbol):
            logger.info(
                "PIPE %s: NO ENTRY — already holding an open position in %s "
                "(one position per coin; it won't re-buy until this one closes)",
                symbol, symbol,
            )
            return

        # [Q2] risk gate. Caps are GLOBAL — counted across every coin in the
        # watchlist — so more symbols never means more concurrent risk.
        open_n = state.count_open()
        pnl_today = state.today_realized_pnl()
        start_eq = state.today_start_equity()
        streak = state.consecutive_losses()
        orders_n = state.orders_today()
        decision = risk.check_can_trade(
            open_positions=open_n,
            realized_pnl_today=pnl_today,
            start_equity_today=start_eq,
            consecutive_losses=streak,
            orders_today=orders_n,
        )
        logger.info(
            "PIPE %s: check_can_trade allowed=%s reason=%s "
            "[open=%d/%d pnl_today=%.4f start_eq=%s streak=%d/%d orders_today=%d/%d]",
            symbol, decision.allowed, decision.reason,
            open_n, config.RISK.max_open_positions, pnl_today,
            f"{start_eq:.4f}" if start_eq is not None else "None",
            streak, config.RISK.max_consecutive_losses,
            orders_n, config.RISK.max_daily_orders,
        )
        if not decision.allowed:
            return

        # [Q3] available balance used for sizing + [Q4]/[Q5] the size math.
        equity = await self._equity()
        stop, target = risk.stop_and_target(price, atr)
        bd = risk.size_breakdown(
            equity=equity, entry_price=price, stop_price=stop, available_quote=equity
        )
        logger.info(
            "PIPE %s: sizing equity=%.8f entry=%.8f stop=%.8f risk/unit=%.8f "
            "risk_budget=%.8f risk_size=%.8f cap_size=%.8f affordable=%.8f -> size=%.8f (%s)",
            symbol, equity, price, stop, bd["risk_per_unit"], bd["risk_budget"],
            bd["risk_size"], bd["cap_size"], bd["affordable"], bd["size"], bd["reason"],
        )
        size = bd["size"]
        if size <= 0:
            logger.warning("PIPE %s: NO ORDER — position size is 0. Why: %s", symbol, bd["reason"])
            return

        # [Q6] execution.open_long is called. [Q7]/[Q9] handled inside it and
        # in kucoin_client.place_market_order (request + full raw response).
        logger.info(
            "PIPE %s: submitting -> execution.open_long(size=%.8f entry=%.8f stop=%.8f "
            "target=%.8f live=%s)", symbol, size, price, stop, target, self.live,
        )
        try:
            _id, res = await execution.open_long(symbol, size, price, stop, target, live=self.live)
        except execution.ValidationError as exc:
            logger.warning("PIPE %s: NO ORDER — pre-trade validation rejected it: %s", symbol, exc)
            return
        real = res.get("dryRun") is False
        logger.info(
            "PIPE %s: open_long returned real_order=%s orderId=%s dryRun=%s",
            symbol, real, res.get("orderId"), res.get("dryRun"),
        )
        tag = "" if real else "PAPER "
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
        """Fetch data for one coin, then manage its position and maybe enter.

        SIGNAL GENERATION uses the regime framework (bull -> trend, sideways ->
        mean reversion, high_vol -> breakout, bear -> cash). Everything after
        the signal — ATR stops, risk checks, sizing, execution — is unchanged.
        Every branch is logged (prefix ``PIPE``) so the pipeline traces end to
        end: signal -> risk -> sizing -> order submission.
        """
        rows = await kucoin_client.fetch_ohlcv(symbol, config.KLINE_TYPE, limit=config.REGIME_CANDLES)
        # [Q8] first possible exit: no usable candle data this tick.
        if len(rows) < 2:
            logger.info("PIPE %s: no candle data (%d rows) — HOLD", symbol, len(rows))
            return
        df = regime_signal.frame_from_ohlcv(rows)
        closes = df["close"].tolist()
        price = closes[-1]
        # Stops still use the engine's own ATR over (high, low, close) — the
        # execution pipeline is unchanged, only the signal below is new.
        atr = _atr(list(zip(df["high"].tolist(), df["low"].tolist(), df["close"].tolist())))
        # [Q1] is a BUY signal generated? Now from the REGIME framework.
        signal, regime = regime_signal.signal_from_frame(df)
        logger.info(
            "PIPE %s: regime=%s signal=%s price=%.8f atr=%.8f bars=%d",
            symbol, regime, signal.value, price, atr, len(df),
        )
        # Requirement: while warming-up (not enough history to classify a
        # regime) submit NOTHING — no entries and no exits. Guarantees no order
        # goes out on a HOLD/warming-up tick.
        if regime == "warming-up":
            logger.info("PIPE %s: warming-up — no orders submitted (need more candles)", symbol)
            return
        await self._manage_open(symbol, price, atr, exit_signal=(signal == Signal.SELL))
        if signal == Signal.BUY:
            await self._maybe_open(symbol, price, atr)
        else:
            # [Q8] no entry this tick: the current regime's entry rule didn't
            # fire (or the regime is bear -> cash). Logged so it's never silent.
            logger.info(
                "PIPE %s: no entry attempted — regime=%s signal=%s "
                "(bull->trend, sideways->mean-reversion, high_vol->breakout, bear->cash)",
                symbol, regime, signal.value,
            )

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

        # Reconcile recovered positions against the exchange BEFORE trading, so a
        # phantom can't trigger a SELL on the first tick.
        await self._reconcile_positions()

        recovered = state.open_positions()
        mode = "LIVE 💸" if self.live else "PAPER (simulated)"
        watchlist = ", ".join(self.symbols)
        await notify.send(
            f"🤖 Engine started — {mode} — {len(self.symbols)} coin(s): {watchlist}. "
            f"Strategy: regime (bull→trend / sideways→mean-rev / high-vol→breakout / bear→cash). "
            f"{reason}. Recovered {len(recovered)} open position(s)."
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
