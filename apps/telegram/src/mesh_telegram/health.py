"""A minimal liveness HTTP surface for the docker healthcheck.

Runs uvicorn on a daemon thread so the bot's long-polling loop can own the main
thread (and its signal handlers). ``/healthz`` is process liveness only — it
returns 200 as soon as the process is up, even while the bot idles waiting for a
token, so a missing config doesn't crash-loop the container.
"""
from __future__ import annotations

import threading

import structlog
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = structlog.get_logger(__name__)


def _build_app(polling: bool) -> Starlette:
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "polling": polling})

    return Starlette(routes=[Route("/healthz", healthz)])


def start_health_server(host: str, port: int, *, polling: bool) -> threading.Thread:
    """Start the health server on a daemon thread and return it."""

    def _serve() -> None:
        config = uvicorn.Config(
            _build_app(polling), host=host, port=port, log_level="warning"
        )
        server = uvicorn.Server(config)
        # uvicorn skips installing signal handlers off the main thread, so this
        # is safe to run here.
        server.run()

    thread = threading.Thread(target=_serve, name="health", daemon=True)
    thread.start()
    logger.info("health_server_started", host=host, port=port)
    return thread
