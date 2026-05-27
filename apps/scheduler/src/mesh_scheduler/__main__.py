"""Entry point: ``mesh-scheduler`` — runs the blocking APScheduler loop."""
from __future__ import annotations

import structlog

from mesh_scheduler.scheduler import build_scheduler, register_jobs

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ]
)

logger = structlog.get_logger(__name__)


def main() -> None:
    scheduler = build_scheduler()
    register_jobs(scheduler)
    logger.info("scheduler_starting")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("scheduler_stopping")


if __name__ == "__main__":
    main()
