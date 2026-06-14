"""The Telegram application: command/message handlers + the daily-brief job."""
from __future__ import annotations

import structlog
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from mesh_telegram.client import BriefingUnavailable, MeshApiClient, MeshApiError
from mesh_telegram.config import Config
from mesh_telegram.format import format_answer, format_briefing

logger = structlog.get_logger(__name__)

_CONFIG_KEY = "config"
_CLIENT_KEY = "client"

_HELP = (
    "I'm your Agent Mesh bridge.\n\n"
    "• Send me any question and I'll answer it from the mesh's knowledge "
    "(beliefs, claims, entities), with citations.\n"
    "• /brief — get today's daily brief now.\n"
    "• /help — show this message."
)


def _config(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.application.bot_data[_CONFIG_KEY]  # type: ignore[no-any-return]


def _client(context: ContextTypes.DEFAULT_TYPE) -> MeshApiClient:
    return context.application.bot_data[_CLIENT_KEY]  # type: ignore[no-any-return]


def _deny_message(chat_id: int) -> str:
    return (
        "You're not authorized to use this bot yet.\n\n"
        f"Your chat id is: {chat_id}\n"
        "Add it to TELEGRAM_ALLOWED_CHAT_IDS in the deployment's .env "
        "(comma-separated) and restart the telegram-bot service."
    )


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    cfg = _config(context)
    if not cfg.is_allowed(chat.id):
        await context.bot.send_message(chat.id, _deny_message(chat.id))
        return
    await context.bot.send_message(chat.id, _HELP)


async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    await context.bot.send_message(chat.id, _HELP)


async def on_brief(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    cfg = _config(context)
    if not cfg.is_allowed(chat.id):
        await context.bot.send_message(chat.id, _deny_message(chat.id))
        return
    await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
    try:
        briefing = await _client(context).briefing(cfg.field_slug)
    except BriefingUnavailable as exc:
        await context.bot.send_message(chat.id, f"No brief available: {exc.detail}")
        return
    except MeshApiError as exc:
        await context.bot.send_message(chat.id, f"Couldn't fetch the brief: {exc}")
        return
    text = format_briefing(briefing, wiki_url=cfg.wiki_url, field_slug=cfg.field_slug)
    await context.bot.send_message(chat.id, text, disable_web_page_preview=True)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None or not message.text:
        return
    cfg = _config(context)
    if not cfg.is_allowed(chat.id):
        await context.bot.send_message(chat.id, _deny_message(chat.id))
        return

    question = message.text.strip()
    if not question:
        return
    await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
    try:
        answer = await _client(context).ask(question, cfg.field_slug)
    except MeshApiError as exc:
        await context.bot.send_message(chat.id, f"Sorry, I couldn't answer: {exc}")
        return
    await context.bot.send_message(
        chat.id, format_answer(answer), disable_web_page_preview=True
    )


async def send_daily_brief(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: push the brief to every allow-listed chat."""
    cfg = _config(context)
    if not cfg.allowed_chat_ids:
        logger.warning("daily_brief_no_recipients")
        return
    try:
        briefing = await _client(context).briefing(cfg.field_slug)
    except BriefingUnavailable as exc:
        logger.info("daily_brief_unavailable", detail=exc.detail)
        return
    except MeshApiError as exc:
        logger.warning("daily_brief_failed", error=str(exc))
        return
    text = format_briefing(briefing, wiki_url=cfg.wiki_url, field_slug=cfg.field_slug)
    for chat_id in cfg.allowed_chat_ids:
        try:
            await context.bot.send_message(
                chat_id, text, disable_web_page_preview=True
            )
        except Exception as exc:  # one bad chat shouldn't stop the rest
            logger.warning("daily_brief_send_failed", chat_id=chat_id, error=str(exc))


def build_application(cfg: Config) -> Application:  # type: ignore[type-arg]
    """Wire up the bot: handlers, the shared API client, and the daily job."""

    async def _post_init(app: Application) -> None:  # type: ignore[type-arg]
        app.bot_data[_CLIENT_KEY] = MeshApiClient(
            cfg.api_url, ask_timeout=cfg.ask_timeout
        )

    async def _post_shutdown(app: Application) -> None:  # type: ignore[type-arg]
        client = app.bot_data.get(_CLIENT_KEY)
        if client is not None:
            await client.aclose()

    app = (
        ApplicationBuilder()
        .token(cfg.token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data[_CONFIG_KEY] = cfg

    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("help", on_help))
    app.add_handler(CommandHandler("brief", on_brief))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    if cfg.briefing_enabled:
        if app.job_queue is None:
            logger.warning("job_queue_unavailable_briefing_disabled")
        else:
            app.job_queue.run_daily(
                send_daily_brief,
                time=cfg.briefing_time,
                name="daily_brief",
            )
            logger.info(
                "daily_brief_scheduled",
                hour=cfg.briefing_hour,
                minute=cfg.briefing_minute,
                tz=str(cfg.tz),
            )

    return app
