"""Telegram notifications via the Bot API — decoupled from the chat app so
the trading engine can send alerts without running the full Telegram
Application. Never logs the bot token.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import httpx

from bot import config

logger = logging.getLogger(__name__)


async def send(text: str) -> None:
    """Best-effort message to the owner. Silently degrades if unconfigured;
    a notification failure must never break trading."""
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_OWNER_ID):
        logger.info("notify (telegram not configured): %s", text)
        return
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={"chat_id": config.TELEGRAM_OWNER_ID, "text": text})
    except httpx.HTTPError as exc:
        logger.warning("telegram notify failed: %s", exc)  # note: never logs the token


async def _test() -> None:
    """`python -m bot.notify [message]` — send a test alert to your phone
    and report clearly whether it worked."""
    message = " ".join(sys.argv[1:]) or (
        "✅ Test from your Medik bot — if this reached your phone, notifications work!"
    )
    if not config.TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN is not set in .env.")
        return
    if not config.TELEGRAM_OWNER_ID:
        print("❌ TELEGRAM_OWNER_ID is not set in .env.")
        print("   Message @userinfobot on Telegram for your numeric id, add it, and retry.")
        return

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url, json={"chat_id": config.TELEGRAM_OWNER_ID, "text": message}
            )
    except httpx.HTTPError as exc:
        print(f"❌ Could not reach Telegram: {exc}")
        return

    if resp.status_code == 200 and resp.json().get("ok"):
        print(f"✅ Sent! Check your phone's Telegram app (chat {config.TELEGRAM_OWNER_ID}).")
    else:
        # Telegram's 'description' explains failures like a wrong id or a
        # chat you haven't started with the bot yet.
        try:
            detail = resp.json().get("description", resp.text)
        except ValueError:
            detail = resp.text
        print(f"❌ Telegram error {resp.status_code}: {detail}")


if __name__ == "__main__":
    asyncio.run(_test())
