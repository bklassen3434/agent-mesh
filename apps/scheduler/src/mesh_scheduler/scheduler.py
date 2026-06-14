"""APScheduler wiring for the mesh (Phase 9).

The scheduler is a *trigger*, not an orchestrator: each job shells out to
the same CLI entry point a human would run (``mesh-ingest``,
``mesh-skeptic``). All DB writes happen on the coordinator/sweep
side, exactly as when the user runs ``make ingest`` by hand.

Phase 9 changes the control model:

* Schedule config (interval + enabled) is read from the Postgres
  ``schedules`` table, not env-var crons. ``interval_hours`` drives an
  APScheduler ``IntervalTrigger``.
* ``SchedulerManager`` owns a non-blocking ``BackgroundScheduler``, tracks
  per-job runtime state (running / last run / outcome), and exposes the
  operations the HTTP control surface needs: ``status()``, ``trigger()``,
  ``reconcile()``.
* ``reconcile()`` re-reads Postgres and applies interval/enabled changes
  to the live jobs **without a restart**. It runs both on a 30s poll and
  on an explicit ``/scheduler/reload`` signal from the API — the signal
  makes a UI change apply near-instantly, the poll is the safety net.

``configured_cron_triggers`` is retained unchanged for the legacy
``/status`` HTML page, which Phase 9 intentionally leaves as-is.
"""
from __future__ import annotations

import os
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from mesh_a2a.schedules import (
    DEFAULT_INTERVALS,
    SchedulesUnavailable,
    ensure_schedules_table,
    list_schedules,
)
from mesh_models.field import DEFAULT_FIELD_ID
from mesh_models.schedule import SchedulerJobStatus

logger = structlog.get_logger(__name__)


def _aps_id(job_id: str, field_id: str) -> str:
    """Build the APScheduler job id / state key for a (job_id, field_id).

    The default field keeps the bare ``job_id`` so existing single-field
    deployments (and their job ids / state keys) are byte-for-byte
    unchanged; additional fields get a ``job_id:field_id`` suffix.
    """
    if field_id == DEFAULT_FIELD_ID:
        return job_id
    return f"{job_id}:{field_id}"

# job_id → CLI command. The set of job_ids the scheduler will run.
JOB_COMMANDS: dict[str, list[str]] = {
    "ingest": ["uv", "run", "mesh-ingest", "--a2a"],
    "skeptic": ["uv", "run", "mesh-skeptic"],
    # Phase 16c: offline memory consolidation (distills episodic history into
    # procedural heuristics). Fired by the existing scheduler — no new container.
    "memory_consolidation": ["uv", "run", "mesh-consolidate-memory"],
    # Phase 19: offline belief consolidation (semantic dedup/merge + staleness
    # decay/archival). Iterates active fields internally — no --field flag.
    "belief_consolidation": ["uv", "run", "mesh-consolidate-beliefs"],
    # Phase 22d: proactive autonomous discovery (gap/trend analysis → opens
    # discovery investigations → dispatches real search). No new container.
    "discovery": ["uv", "run", "mesh-discover"],
}

_RECONCILE_JOB_ID = "_reconcile"
_RECONCILE_INTERVAL_SECONDS = 30


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def configured_cron_triggers() -> dict[str, CronTrigger]:
    """Legacy helper for the /status HTML page (kept as-is in Phase 9).

    Reads the env-var cron expressions the pre-Phase-9 scheduler used.
    Decoupled from the live interval-based schedule now driven by Postgres.
    """
    return {
        "ingest": CronTrigger.from_crontab(_env("MESH_SCHEDULE_PIPELINE_CRON", "0 */6 * * *")),
        "skeptic": CronTrigger.from_crontab(_env("MESH_SCHEDULE_SWEEP_CRON", "0 3 * * *")),
    }


@dataclass
class _JobState:
    """In-memory runtime state for one job. Mirrors the DB's interval +
    enabled, plus run bookkeeping APScheduler doesn't track itself."""

    interval_hours: int
    enabled: bool
    field_id: str = DEFAULT_FIELD_ID
    running: bool = False
    last_run_at: datetime | None = None
    last_outcome: str | None = None  # "running" | "completed" | "failed"
    last_run_id: str | None = None


class SchedulerManager:
    """Owns the BackgroundScheduler and the job runtime state.

    Thread-safety: APScheduler fires jobs on a thread pool and manual runs
    spawn their own threads, while the HTTP server reads state from the
    asyncio thread. A single lock guards ``_state`` mutations.
    """

    def __init__(self) -> None:
        self._scheduler = BackgroundScheduler()
        self._lock = threading.Lock()
        self._state: dict[str, _JobState] = {}

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        rows: list[tuple[str, str, int, bool]] = []
        try:
            ensure_schedules_table()
            rows = [
                (s.job_id, s.field_id, s.interval_hours, s.enabled)
                for s in list_schedules()
            ]
        except SchedulesUnavailable:
            logger.warning("schedules_postgres_unavailable_using_defaults")

        if rows:
            for job_id, field_id, hours, enabled in rows:
                if job_id in JOB_COMMANDS:
                    self._register(job_id, field_id, hours, enabled)
        else:
            # No Postgres config: register the default-field jobs from the
            # built-in interval defaults so the scheduler still functions.
            for job_id, default_hours in DEFAULT_INTERVALS.items():
                self._register(job_id, DEFAULT_FIELD_ID, default_hours, True)

        self._scheduler.add_job(
            self._reconcile_safe,
            trigger=IntervalTrigger(seconds=_RECONCILE_INTERVAL_SECONDS),
            id=_RECONCILE_JOB_ID,
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info("scheduler_started", jobs=sorted(self._state))

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def _register(self, job_id: str, field_id: str, hours: int, enabled: bool) -> None:
        aps_id = _aps_id(job_id, field_id)
        self._scheduler.add_job(
            self._scheduled_fire,
            args=[job_id, field_id],
            trigger=IntervalTrigger(hours=hours),
            id=aps_id,
            name=f"Mesh {aps_id}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        if not enabled:
            self._scheduler.pause_job(aps_id)
        with self._lock:
            self._state[aps_id] = _JobState(
                interval_hours=hours, enabled=enabled, field_id=field_id
            )

    # ── execution ────────────────────────────────────────────────────────
    def _begin(
        self, job_id: str, triggered_by: str, field_id: str = DEFAULT_FIELD_ID
    ) -> str | None:
        """Claim a run slot. Returns a fresh run_id, or None if busy."""
        aps_id = _aps_id(job_id, field_id)
        with self._lock:
            st = self._state.get(aps_id)
            if st is None or st.running:
                return None
            run_id = str(uuid.uuid4())
            st.running = True
            st.last_run_id = run_id
            st.last_run_at = datetime.now(UTC)
            st.last_outcome = "running"
        logger.info(
            "run_starting", job_id=job_id, field_id=field_id, run_id=run_id,
            triggered_by=triggered_by,
        )
        return run_id

    def _run_blocking(
        self, job_id: str, run_id: str, triggered_by: str, field_id: str = DEFAULT_FIELD_ID
    ) -> None:
        aps_id = _aps_id(job_id, field_id)
        cmd = list(JOB_COMMANDS[job_id])
        # Only the ingest command accepts --field; sweep/consolidation
        # process all fields / their own scope.
        if job_id == "ingest":
            cmd += ["--field", field_id]
        env = dict(os.environ)
        env["MESH_TRIGGERED_BY"] = triggered_by
        env["MESH_RUN_ID"] = run_id  # coordinator/sweep honor this as their run id
        env["MESH_PIPELINE_FIELD"] = field_id
        outcome = "completed"
        try:
            result = subprocess.run(cmd, env=env, check=False)
            if result.returncode != 0:
                outcome = "failed"
        except Exception as exc:
            logger.error(
                "run_errored", job_id=job_id, field_id=field_id, run_id=run_id, error=str(exc)
            )
            outcome = "failed"
        with self._lock:
            st = self._state.get(aps_id)
            if st is not None:
                st.running = False
                st.last_outcome = outcome
        logger.info(
            "run_finished", job_id=job_id, field_id=field_id, run_id=run_id, outcome=outcome
        )

    def _scheduled_fire(self, job_id: str, field_id: str = DEFAULT_FIELD_ID) -> None:
        run_id = self._begin(job_id, "scheduled", field_id)
        if run_id is None:
            logger.info(
                "scheduled_run_skipped_already_running", job_id=job_id, field_id=field_id
            )
            return
        self._run_blocking(job_id, run_id, "scheduled", field_id)

    def trigger(
        self, job_id: str, field_id: str = DEFAULT_FIELD_ID
    ) -> tuple[str, datetime] | None:
        """Start an immediate manual run. Returns (run_id, triggered_at), or
        None if a run for this (job, field) is already in progress. Raises
        KeyError for an unknown job."""
        if job_id not in JOB_COMMANDS:
            raise KeyError(job_id)
        run_id = self._begin(job_id, "manual", field_id)
        if run_id is None:
            return None
        aps_id = _aps_id(job_id, field_id)
        with self._lock:
            triggered_at = self._state[aps_id].last_run_at or datetime.now(UTC)
        threading.Thread(
            target=self._run_blocking,
            args=(job_id, run_id, "manual", field_id),
            daemon=True,
        ).start()
        return run_id, triggered_at

    # ── introspection ────────────────────────────────────────────────────
    def status(self) -> list[SchedulerJobStatus]:
        out: list[SchedulerJobStatus] = []
        with self._lock:
            items = list(self._state.items())
        for aps_id, st in items:
            # The aps_id is the bare job_id for the default field, else
            # ``job_id:field_id`` — recover the logical job_id either way.
            if st.field_id == DEFAULT_FIELD_ID:
                job_id = aps_id
            else:
                job_id = aps_id.removesuffix(f":{st.field_id}")
            job = self._scheduler.get_job(aps_id)
            next_run = getattr(job, "next_run_time", None) if job else None
            if st.running:
                state = "running"
            elif not st.enabled:
                state = "disabled"
            else:
                state = "idle"
            out.append(
                SchedulerJobStatus(
                    job_id=job_id,
                    field_id=st.field_id,
                    next_run_at=next_run,
                    last_run_at=st.last_run_at,
                    state=state,
                )
            )
        return out

    # ── reconcile ────────────────────────────────────────────────────────
    def reconcile(self) -> None:
        """Apply the Postgres schedule config to the live jobs.

        Only acts on actual transitions: pause/resume are gated on the
        enabled flag changing (a gratuitous ``resume_job`` would push the
        next fire time forward every poll). ``reschedule_job`` resumes a
        paused job, so a disabled job whose interval changed is re-paused.
        """
        for s in list_schedules():
            if s.job_id not in JOB_COMMANDS:
                continue
            aps_id = _aps_id(s.job_id, s.field_id)
            with self._lock:
                st = self._state.get(aps_id)
            if st is None:
                self._register(s.job_id, s.field_id, s.interval_hours, s.enabled)
                continue

            interval_changed = s.interval_hours != st.interval_hours
            if interval_changed:
                self._scheduler.reschedule_job(
                    aps_id, trigger=IntervalTrigger(hours=s.interval_hours)
                )
                with self._lock:
                    st.interval_hours = s.interval_hours
                logger.info(
                    "job_rescheduled", job_id=s.job_id, field_id=s.field_id,
                    interval_hours=s.interval_hours,
                )

            if s.enabled != st.enabled:
                if s.enabled:
                    self._scheduler.resume_job(aps_id)
                else:
                    self._scheduler.pause_job(aps_id)
                with self._lock:
                    st.enabled = s.enabled
                logger.info(
                    "job_enabled_changed", job_id=s.job_id, field_id=s.field_id,
                    enabled=s.enabled,
                )
            elif interval_changed and not s.enabled:
                # reschedule_job just resumed a job that should stay disabled.
                self._scheduler.pause_job(aps_id)

    def _reconcile_safe(self) -> None:
        try:
            self.reconcile()
        except SchedulesUnavailable:
            pass
        except Exception as exc:
            logger.warning("reconcile_failed", error=str(exc))
