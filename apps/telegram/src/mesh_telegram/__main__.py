"""Entry point: ``mesh-telegram``.

Starts the liveness health server on a daemon thread, then runs the bot's
long-polling loop on the main thread. With no token configured it stays up and
idle (logging a warning) rather than crash-looping, so the operator can add the
token and restart cleanly.
"""
from __future__ import annotations

import threading

import structlog

from mesh_telegram.bot import build_application
from mesh_telegram.config import Config
from mesh_telegram.health import start_health_server

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ]
)

logger = structlog.get_logger(__name__)


def main() -> None:
    cfg = Config.from_env()

    if not cfg.token:
        logger.error("no_token_set", hint="set TELEGRAM_BOT_TOKEN in .env")
        start_health_server(cfg.health_host, cfg.health_port, polling=False)
        # Stay alive (and healthy as a process) so the container doesn't
        # crash-loop; the operator sets the token then restarts.
        threading.Event().wait()
        return

    start_health_server(cfg.health_host, cfg.health_port, polling=True)
    logger.info(
        "telegram_bot_starting",
        api_url=cfg.api_url,
        field=cfg.field_slug,
        allowed_chats=len(cfg.allowed_chat_ids),
        briefing_enabled=cfg.briefing_enabled,
    )
    if not cfg.allowed_chat_ids:
        logger.warning(
            "no_allowed_chat_ids",
            hint="message the bot and use the chat id it replies with to set "
            "TELEGRAM_ALLOWED_CHAT_IDS",
        )

    app = build_application(cfg)
    # run_polling owns the main-thread event loop and installs its own signal
    # handlers for clean shutdown.
    app.run_polling()


if __name__ == "__main__":
    main()
