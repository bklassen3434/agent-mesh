"""APScheduler wiring for the mesh.

In-process scheduler with a `MemoryJobStore` and a single
`ThreadPoolExecutor`. Two jobs (pipeline + sweep) are registered at
startup with cron expressions from env vars. Each job invokes the
existing CLI entry point as a subprocess — the scheduler is a
*trigger*, not an orchestrator. All DB writes happen on the
coordinator/sweep side, exactly as they do when the user runs
``make pipeline`` or ``make skeptic`` by hand.
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from typing import Any

import structlog
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

logger = structlog.get_logger(__name__)


JobRunner = Callable[[], None]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def configured_cron_triggers() -> dict[str, CronTrigger]:
    """Build the same CronTrigger objects ``register_jobs`` would use.

    Read-only helper for ``mesh.cli schedule status`` so it can ask each
    trigger for its next-fire-time without spinning up an actual
    scheduler (BlockingScheduler.start would block the CLI thread).
    """
    return {
        "pipeline": CronTrigger.from_crontab(_env("MESH_SCHEDULE_PIPELINE_CRON", "0 */6 * * *")),
        "skeptic_sweep": CronTrigger.from_crontab(_env("MESH_SCHEDULE_SWEEP_CRON", "0 3 * * *")),
    }


def _run_subprocess(cmd: list[str], triggered_by: str) -> None:
    """Run a CLI subprocess inheriting the scheduler's environment.

    ``MESH_TRIGGERED_BY`` is layered on top so the coordinator/sweep
    tag their pipeline_runs row with ``triggered_by='scheduled'``.
    """
    env = dict(os.environ)
    env["MESH_TRIGGERED_BY"] = triggered_by
    logger.info("scheduled_run_starting", cmd=cmd, triggered_by=triggered_by)
    try:
        result = subprocess.run(cmd, env=env, check=False, capture_output=False)
        logger.info(
            "scheduled_run_finished",
            cmd=cmd,
            returncode=result.returncode,
            triggered_by=triggered_by,
        )
    except Exception as exc:
        logger.error("scheduled_run_errored", cmd=cmd, error=str(exc))


def _pipeline_job() -> None:
    _run_subprocess(["uv", "run", "mesh-pipeline", "--a2a"], triggered_by="scheduled")


def _sweep_job() -> None:
    _run_subprocess(["uv", "run", "mesh-skeptic-sweep"], triggered_by="scheduled")


def build_scheduler() -> BlockingScheduler:
    """Build a single-process scheduler with sensible defaults.

    Default ``MemoryJobStore`` and ``ThreadPoolExecutor`` are kept on
    purpose — DuckDB isn't a job store, and the audit log lives in
    ``pipeline_runs``. Misfires use APScheduler defaults (no replay).
    """
    return BlockingScheduler()


def register_jobs(
    scheduler: BlockingScheduler,
    *,
    pipeline_runner: JobRunner | None = None,
    sweep_runner: JobRunner | None = None,
) -> dict[str, Any]:
    """Register the pipeline + sweep jobs against env-configured crons.

    Returns the registered job objects keyed by id so callers (tests,
    the ``schedule status`` command) can introspect them.
    """
    pipeline_cron = _env("MESH_SCHEDULE_PIPELINE_CRON", "0 */6 * * *")
    sweep_cron = _env("MESH_SCHEDULE_SWEEP_CRON", "0 3 * * *")

    jobs: dict[str, Any] = {}
    jobs["pipeline"] = scheduler.add_job(
        pipeline_runner or _pipeline_job,
        trigger=CronTrigger.from_crontab(pipeline_cron),
        id="pipeline",
        name="Mesh ingestion pipeline",
        replace_existing=True,
    )
    jobs["skeptic_sweep"] = scheduler.add_job(
        sweep_runner or _sweep_job,
        trigger=CronTrigger.from_crontab(sweep_cron),
        id="skeptic_sweep",
        name="Skeptic falsification sweep",
        replace_existing=True,
    )
    logger.info(
        "jobs_registered",
        pipeline_cron=pipeline_cron,
        sweep_cron=sweep_cron,
    )
    return jobs
