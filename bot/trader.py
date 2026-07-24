"""Autonomous trading loop: fetch candles -> compute signal -> act.

THE DEFAULT IS DRY-RUN. While config.LIVE_TRADING is false (the default)
this loop logs and announces the trades it *would* make and places
nothing at all. Only when you deliberately set LIVE_TRADING=true in .env
does it send real market orders — and even then it is bounded by the
caps in config (small funds per order, a max number of orders per day,
one symbol only).

Nothing here runs on its own: the loop starts only when the owner sends
/autotrade on, and stops on /autotrade off or bot shutdown.
"""

import asyncio
import logging
import time

from bot import config, kucoin_client
from bot.strategy import Signal, crossover_signal

logger = logging.getLogger(__name__)


class Trader:
    def __init__(self, notify=None):
        self._task: asyncio.Task | None = None
        self._notify = notify  # async callable(str) that sends a Telegram message
        # In-memory position tracking: the base amount this bot has bought
        # and not yet sold. Approximate and reset on restart — fine for a
        # starter bot, a production one would persist real fills.
        self.position_size = 0.0
        self.orders_today = 0
        self._orders_day: str | None = None
        self.last_signal = Signal.HOLD

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        if self.running:
            return False
        self._task = asyncio.create_task(self._run())
        return True

    async def stop(self) -> bool:
        if not self.running:
            return False
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        return True

    async def _announce(self, message: str) -> None:
        logger.info(message)
        if self._notify is not None:
            try:
                await self._notify(message)
            except Exception:  # a notify failure must never kill the loop
                logger.exception("failed to send trade notification")

    def _roll_daily_counter(self) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != self._orders_day:
            self._orders_day = today
            self.orders_today = 0

    async def _run(self) -> None:
        mode = "LIVE 💸" if config.LIVE_TRADING else "DRY-RUN (no real orders)"
        await self._announce(
            f"🤖 Auto-trader started — {mode} — {config.TRADE_SYMBOL}, "
            f"MA({config.FAST_MA}/{config.SLOW_MA}), checking every "
            f"{config.TRADE_INTERVAL_SECONDS:.0f}s."
        )
        try:
            while True:
                try:
                    await self._tick()
                except kucoin_client.KucoinError as exc:
                    await self._announce(f"⚠️ Trade check skipped: {exc}")
                await asyncio.sleep(config.TRADE_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            await self._announce("🛑 Auto-trader stopped.")
            raise

    async def _tick(self) -> None:
        closes = await kucoin_client.fetch_klines(config.TRADE_SYMBOL, config.KLINE_TYPE)
        if not closes:
            return
        signal = crossover_signal(closes, config.FAST_MA, config.SLOW_MA)
        price = closes[-1]

        # Only act on a fresh, non-HOLD signal.
        if signal == Signal.HOLD or signal == self.last_signal:
            return
        self.last_signal = signal

        self._roll_daily_counter()
        if self.orders_today >= config.MAX_DAILY_ORDERS:
            await self._announce(
                f"⚠️ Daily order cap ({config.MAX_DAILY_ORDERS}) reached — "
                f"ignoring {signal.value}."
            )
            return

        base, quote = config.TRADE_SYMBOL.split("-")
        if signal == Signal.BUY:
            if self.position_size > 0:
                return  # already holding a position
            result = await kucoin_client.place_market_order(
                config.TRADE_SYMBOL, "buy", funds=config.TRADE_FUNDS_PER_ORDER
            )
            self.position_size = float(config.TRADE_FUNDS_PER_ORDER) / price
            self.orders_today += 1
            tag = "DRY-RUN " if result.get("dryRun") else ""
            await self._announce(
                f"🟢 {tag}BUY ~{config.TRADE_FUNDS_PER_ORDER} {quote} of {base} @ ~{price}"
            )
        elif signal == Signal.SELL:
            if self.position_size <= 0:
                return  # nothing bought to sell
            size = round(self.position_size, 8)
            result = await kucoin_client.place_market_order(
                config.TRADE_SYMBOL, "sell", size=size
            )
            self.position_size = 0.0
            self.orders_today += 1
            tag = "DRY-RUN " if result.get("dryRun") else ""
            await self._announce(f"🔴 {tag}SELL {size} {base} @ ~{price}")
