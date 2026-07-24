"""Telegram notifications via the Bot API — decoupled from the chat app so
the trading engine can send alerts without running the full Telegram
Application. Never logs the bot token.
"""

from __future__ import annotations

import logging

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
