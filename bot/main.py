"""Telegram bot entry point: wires chat commands to the API client.

Run from the repository root with:  python -m bot.main
"""

import html
import logging

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from bot import api_client, config

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
        "Hi! I look up medicine facts from the openFDA drug-label API.\n\n"
        "Try: /drug ibuprofen\n"
        "All commands: /help"
    )


async def help_command(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/drug <name> — look up a medicine by brand or generic name\n"
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


async def _shutdown(_: Application) -> None:
    await api_client.aclose()


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
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("drug", drug))

    logger.info("Bot starting (long polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
