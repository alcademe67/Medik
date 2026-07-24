"""Phone notifications for the bot — no Telegram required.

Supports whichever channel you configure in .env:
  * ntfy     — free push app; set NTFY_TOPIC (no account, no API keys)
  * Telegram — set TELEGRAM_BOT_TOKEN + TELEGRAM_OWNER_ID

`send()` delivers to EVERY configured channel (so you can use one or both).
If none is configured it just logs. Secrets are never logged.

Test it:  python -m bot.notify [your message]
"""

from __future__ import annotations

import asyncio
import logging
import sys

import httpx

from bot import config

logger = logging.getLogger(__name__)

NTFY_TITLE = "Medik trading bot"  # ASCII only (HTTP header)


async def _send_ntfy(text: str) -> None:
    url = f"{config.NTFY_SERVER}/{config.NTFY_TOPIC}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, content=text.encode("utf-8"), headers={"Title": NTFY_TITLE})
    except httpx.HTTPError as exc:
        logger.warning("ntfy notify failed: %s", exc)


async def _send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={"chat_id": config.TELEGRAM_OWNER_ID, "text": text})
    except httpx.HTTPError as exc:
        logger.warning("telegram notify failed: %s", exc)  # never logs the token


def _telegram_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_OWNER_ID)


async def send(text: str) -> None:
    """Best-effort delivery to every configured channel. A failure in one
    channel never breaks trading."""
    delivered = False
    if config.NTFY_TOPIC:
        await _send_ntfy(text)
        delivered = True
    if _telegram_configured():
        await _send_telegram(text)
        delivered = True
    if not delivered:
        logger.info("notify (no channel configured): %s", text)


async def _test() -> None:
    """`python -m bot.notify [message]` — send a test alert and report
    clearly for each configured channel."""
    message = " ".join(sys.argv[1:]) or (
        "✅ Test from your Medik bot — if this reached your phone, notifications work!"
    )
    tested = False

    if config.NTFY_TOPIC:
        tested = True
        url = f"{config.NTFY_SERVER}/{config.NTFY_TOPIC}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(url, content=message.encode("utf-8"), headers={"Title": NTFY_TITLE})
            if r.status_code == 200:
                print(f"✅ ntfy: sent to {url}")
                print(f"   Open the ntfy app (subscribed to topic '{config.NTFY_TOPIC}') — it should be there.")
            else:
                print(f"❌ ntfy error {r.status_code}: {r.text[:200]}")
        except httpx.HTTPError as exc:
            print(f"❌ Could not reach ntfy: {exc}")

    if _telegram_configured():
        tested = True
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(url, json={"chat_id": config.TELEGRAM_OWNER_ID, "text": message})
            if r.status_code == 200 and r.json().get("ok"):
                print(f"✅ Telegram: sent to chat {config.TELEGRAM_OWNER_ID}.")
            else:
                try:
                    detail = r.json().get("description", r.text)
                except ValueError:
                    detail = r.text
                print(f"❌ Telegram error {r.status_code}: {detail}")
        except httpx.HTTPError as exc:
            print(f"❌ Could not reach Telegram: {exc}")

    if not tested:
        print("❌ No notification channel configured in .env.")
        print("   ntfy (easiest): pick any unique name and add it, e.g.")
        print("     NTFY_TOPIC=medik-alerts-8x3k9q")
        print("   then install the 'ntfy' app and subscribe to that same topic.")


if __name__ == "__main__":
    asyncio.run(_test())
