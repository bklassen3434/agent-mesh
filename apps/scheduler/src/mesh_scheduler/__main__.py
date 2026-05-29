"""Entry point: ``mesh-scheduler``.

Starts the BackgroundScheduler (jobs run on their own threads) and serves
the HTTP control surface with uvicorn on the main thread.
"""
from __future__ import annotations

import os

import structlog
import uvicorn

from mesh_scheduler.app import build_app
from mesh_scheduler.scheduler import SchedulerManager

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ]
)

logger = structlog.get_logger(__name__)


def main() -> None:
    manager = SchedulerManager()
    manager.start()
    app = build_app(manager)
    # MESH_BIND_INTERFACE wins for Tailscale-only deployments, mirroring the
    # API/wiki services; default 0.0.0.0 for local + docker-internal access.
    host = (
        os.environ.get("MESH_BIND_INTERFACE")
        or os.environ.get("SCHEDULER_HOST")
        or "0.0.0.0"
    )
    port = int(os.environ.get("SCHEDULER_PORT", "9100"))
    logger.info("scheduler_http_starting", host=host, port=port)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        manager.shutdown()


if __name__ == "__main__":
    main()
