"""Notification routing tests — verify send() fans out to exactly the
channels configured in .env, and to none when nothing is configured.
Network is mocked out; we only test the routing logic.
"""

import asyncio

from bot import config, notify


def test_send_routes_to_configured_channels(monkeypatch):
    calls = []

    async def fake_ntfy(text):
        calls.append(("ntfy", text))

    async def fake_tg(text):
        calls.append(("telegram", text))

    monkeypatch.setattr(notify, "_send_ntfy", fake_ntfy)
    monkeypatch.setattr(notify, "_send_telegram", fake_tg)

    # Only ntfy configured -> only ntfy called.
    monkeypatch.setattr(config, "NTFY_TOPIC", "medik-test")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_OWNER_ID", 0)
    asyncio.run(notify.send("hello"))
    assert calls == [("ntfy", "hello")]

    # Both configured -> both called.
    calls.clear()
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(config, "TELEGRAM_OWNER_ID", 123)
    asyncio.run(notify.send("both"))
    assert ("ntfy", "both") in calls and ("telegram", "both") in calls

    # Nothing configured -> nothing sent (just logs).
    calls.clear()
    monkeypatch.setattr(config, "NTFY_TOPIC", "")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_OWNER_ID", 0)
    asyncio.run(notify.send("nowhere"))
    assert calls == []
