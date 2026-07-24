"""Telegram bot entry point: wires chat commands to the API client.

Run from the repository root with:  python -m bot.main
"""

import html
import logging

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from bot import api_client, config, kucoin_client
from bot.trader import Trader

_trader: Trader | None = None

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Telegram caps messages at 4096 chars and label sections can run for pages.
MAX_SECTION_CHARS = 450

DISCLAIMER = "FDA label data via openFDA — not medical advice."


def _section(title: str, text: str | None) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    if len(text) > MAX_SECTION_CHARS:
        text = text[:MAX_SECTION_CHARS].rstrip() + "…"
    return f"<b>{title}</b>\n{html.escape(text)}"


async def start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hi! I can look up medicine facts (openFDA) and crypto prices (KuCoin).\n\n"
        "Try: /drug ibuprofen\n"
        "Or:  /price BTC\n"
        "All commands: /help"
    )


async def help_command(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/drug <name> — look up a medicine by brand or generic name\n"
        "/price <coin> — live KuCoin price, e.g. /price BTC or /price ETH-EUR\n"
        "/balance — your KuCoin balances (bot owner only)\n"
        "/autotrade on|off|status — run the signal bot (owner only)\n"
        "/help — this message\n\n" + DISCLAIMER
    )


async def drug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /drug <name>, e.g. /drug ibuprofen")
        return

    query = " ".join(context.args)
    await update.effective_chat.send_action(ChatAction.TYPING)

    try:
        info = await api_client.fetch_drug_label(query)
    except api_client.NotFoundError:
        await update.message.reply_text(
            f"No FDA label found for “{query}”. "
            "Try the generic name, e.g. acetaminophen instead of Tylenol."
        )
        return
    except api_client.ApiError:
        await update.message.reply_text(
            "The drug database is not responding right now — please try again in a minute."
        )
        return

    title = info["brand_name"] or query.title()
    if info["generic_name"] and info["generic_name"] != info["brand_name"]:
        title = f"{title} ({info['generic_name']})"

    parts = [f"<b>💊 {html.escape(title)}</b>"]
    for heading, key in (
        ("Purpose", "purpose"),
        ("Used for", "indications"),
        ("Warnings", "warnings"),
        ("Dosage", "dosage"),
    ):
        section = _section(heading, info[key])
        if section:
            parts.append(section)
    parts.append(f"<i>{DISCLAIMER}</i>")

    await update.message.reply_text("\n\n".join(parts), parse_mode=ParseMode.HTML)


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: /price <coin>, e.g. /price BTC or /price ETH-EUR"
        )
        return

    # "/price btc" -> BTC-USDT, "/price eth eur" or "/price ETH-EUR" -> ETH-EUR
    symbol = "-".join(part.upper() for part in context.args)
    if "-" not in symbol:
        symbol += "-USDT"

    await update.effective_chat.send_action(ChatAction.TYPING)
    try:
        ticker = await kucoin_client.fetch_ticker(symbol)
    except kucoin_client.NotFoundError:
        await update.message.reply_text(
            f"KuCoin doesn't list “{symbol}”. Try /price BTC or /price SOL-BTC."
        )
        return
    except kucoin_client.KucoinError:
        await update.message.reply_text(
            "KuCoin is not responding right now — please try again in a minute."
        )
        return

    quote = ticker["symbol"].split("-")[-1]
    lines = [
        f"📈 <b>{html.escape(ticker['symbol'])}</b>",
        f"Price: <b>{html.escape(ticker['last'])}</b> {html.escape(quote)}",
    ]
    if ticker["change_rate"] is not None:
        try:
            pct = float(ticker["change_rate"]) * 100
            lines.append(f"24h: {'▲' if pct >= 0 else '▼'} {pct:+.2f}%")
        except ValueError:
            pass
    if ticker["high"] and ticker["low"]:
        lines.append(f"H {html.escape(ticker['high'])} / L {html.escape(ticker['low'])}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def balance(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    # Account data is private: fail closed until an owner id is configured,
    # and never answer anyone else.
    user = update.effective_user
    if not config.TELEGRAM_OWNER_ID:
        await update.message.reply_text(
            "For safety /balance stays off until TELEGRAM_OWNER_ID is set in .env.\n"
            "Message @userinfobot on Telegram to get your numeric id, add it to "
            ".env, then restart the bot."
        )
        return
    if user is None or user.id != config.TELEGRAM_OWNER_ID:
        await update.message.reply_text("Sorry, /balance only works for the bot's owner.")
        return

    await update.effective_chat.send_action(ChatAction.TYPING)
    try:
        balances = await kucoin_client.fetch_balances()
    except kucoin_client.AuthError:
        await update.message.reply_text(
            "KuCoin rejected the credentials. Re-check KUCOIN_API_KEY, "
            "KUCOIN_API_SECRET and KUCOIN_API_PASSPHRASE in .env "
            "(and KUCOIN_KEY_VERSION, shown when you created the key), "
            "then restart the bot."
        )
        return
    except kucoin_client.KucoinError:
        await update.message.reply_text(
            "KuCoin is not responding right now — please try again in a minute."
        )
        return

    if not balances:
        await update.message.reply_text("No non-zero balances on this KuCoin account.")
        return

    lines = ["💰 KuCoin balances"]
    for entry in balances[:30]:
        line = f"{entry['currency']} ({entry['type']}): {entry['balance']}"
        if entry["available"] != entry["balance"]:
            line += f" — {entry['available']} available"
        lines.append(line)
    if len(balances) > 30:
        lines.append(f"…and {len(balances) - 30} more")

    await update.message.reply_text("\n".join(lines))


def _is_owner(update: Update) -> bool:
    user = update.effective_user
    return bool(config.TELEGRAM_OWNER_ID) and user is not None and (
        user.id == config.TELEGRAM_OWNER_ID
    )


async def autotrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        await update.message.reply_text(
            "Sorry, /autotrade is owner-only. Set TELEGRAM_OWNER_ID in .env "
            "to your Telegram id (from @userinfobot)."
        )
        return

    action = context.args[0].lower() if context.args else "status"

    if action == "on":
        if not (
            config.KUCOIN_API_KEY
            and config.KUCOIN_API_SECRET
            and config.KUCOIN_API_PASSPHRASE
        ):
            await update.message.reply_text("Set your KuCoin keys in .env first.")
            return
        if _trader.start():
            mode = "LIVE 💸 — REAL orders" if config.LIVE_TRADING else "DRY-RUN — no real orders"
            await update.message.reply_text(
                f"Auto-trader started ({mode}).\n"
                f"{config.TRADE_SYMBOL}, MA({config.FAST_MA}/{config.SLOW_MA}), "
                f"every {config.TRADE_INTERVAL_SECONDS:.0f}s.\n"
                "Stop any time with /autotrade off."
            )
        else:
            await update.message.reply_text("Auto-trader is already running.")
    elif action == "off":
        if await _trader.stop():
            await update.message.reply_text("Auto-trader stopped.")
        else:
            await update.message.reply_text("Auto-trader is not running.")
    else:  # status
        mode = "LIVE 💸" if config.LIVE_TRADING else "DRY-RUN"
        await update.message.reply_text(
            f"Auto-trader: {'running' if _trader.running else 'stopped'}\n"
            f"Mode: {mode}\n"
            f"Symbol: {config.TRADE_SYMBOL}\n"
            f"Strategy: MA crossover ({config.FAST_MA}/{config.SLOW_MA})\n"
            f"Funds/order: {config.TRADE_FUNDS_PER_ORDER}\n"
            f"Orders today: {_trader.orders_today}/{config.MAX_DAILY_ORDERS}"
        )


async def _shutdown(_: Application) -> None:
    if _trader is not None:
        await _trader.stop()
    await api_client.aclose()
    await kucoin_client.aclose()


def main() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set — copy .env.example to .env and fill it in."
        )

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_shutdown(_shutdown)
        .build()
    )

    global _trader

    async def _notify(message: str) -> None:
        if config.TELEGRAM_OWNER_ID:
            await app.bot.send_message(chat_id=config.TELEGRAM_OWNER_ID, text=message)

    _trader = Trader(notify=_notify)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("drug", drug))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("autotrade", autotrade))

    logger.info("Bot starting (long polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
