"""Phase 9 scheduler tests.

The scheduler now reads interval/enabled config from Postgres and applies
changes to live jobs without a restart. These tests monkeypatch the
Postgres-backed schedule reads (no real Postgres) and exercise the
SchedulerManager directly: single-flight run claiming, status mapping, and
the reconcile loop's reschedule/pause/resume behavior.

``configured_cron_triggers`` is retained for the legacy /status page and
still tested here.
"""
from __future__ import annotations

from datetime import UTC, datetime

import mesh_scheduler.scheduler as sched_mod
import pytest
from mesh_models.schedule import Schedule
from mesh_scheduler import SchedulerManager, configured_cron_triggers


def _schedules(
    *,
    primary_hours: int = 6,
    beta_hours: int = 24,
    primary_enabled: bool = True,
    beta_enabled: bool = True,
) -> list[Schedule]:
    # The controller is the only job, so multi-job scheduling is now multi-*field*
    # scheduling of the same job (one controller run per field).
    now = datetime.now(UTC)
    return [
        Schedule(job_id="controller", field_id="ai-robotics", interval_hours=primary_hours,
                 enabled=primary_enabled, updated_at=now),
        Schedule(job_id="controller", field_id="beta", interval_hours=beta_hours,
                 enabled=beta_enabled, updated_at=now),
    ]


# ── legacy cron helper (for /status) ─────────────────────────────────────────


def test_default_cron_triggers_match_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MESH_SCHEDULE_CONTROLLER_CRON", raising=False)
    triggers = configured_cron_triggers()
    assert set(triggers.keys()) == {"controller"}
    assert "hour='*/6'" in str(triggers["controller"])


def test_cron_env_overrides_picked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_SCHEDULE_CONTROLLER_CRON", "*/20 * * * *")
    triggers = configured_cron_triggers()
    assert "minute='*/20'" in str(triggers["controller"])


# ── SchedulerManager — run claiming + status ─────────────────────────────────


def test_begin_is_single_flight() -> None:
    m = SchedulerManager()
    m._state["controller"] = sched_mod._JobState(interval_hours=6, enabled=True)
    first = m._begin("controller", "manual")
    assert first is not None
    # A second claim while the first run is in flight is refused.
    assert m._begin("controller", "manual") is None


def test_status_maps_states() -> None:
    m = SchedulerManager()
    m._state["controller"] = sched_mod._JobState(interval_hours=6, enabled=True, running=True)
    m._state["controller:beta"] = sched_mod._JobState(
        interval_hours=24, enabled=False, field_id="beta"
    )
    by = {(s.job_id, s.field_id): s for s in m.status()}
    assert by[("controller", "ai-robotics")].state == "running"
    assert by[("controller", "beta")].state == "disabled"


def test_trigger_unknown_job_raises() -> None:
    m = SchedulerManager()
    with pytest.raises(KeyError):
        m.trigger("does_not_exist")


# ── SchedulerManager — reconcile without a restart ───────────────────────────


def test_reconcile_applies_interval_and_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    schedules = _schedules()
    monkeypatch.setattr(sched_mod, "ensure_schedules_table", lambda: None)
    monkeypatch.setattr(sched_mod, "list_schedules", lambda: schedules)

    m = SchedulerManager()
    m.start()
    try:
        assert m._state["controller"].interval_hours == 6
        # Change the primary field's interval, disable the beta field.
        schedules[:] = _schedules(primary_hours=12, beta_enabled=False)
        m.reconcile()

        by = {(s.job_id, s.field_id): s for s in m.status()}
        assert m._state["controller"].interval_hours == 12
        assert by[("controller", "beta")].state == "disabled"
        # A paused (disabled) job has no scheduled next fire.
        assert by[("controller", "beta")].next_run_at is None
        # Re-enable and confirm it resumes.
        schedules[:] = _schedules(primary_hours=12, beta_enabled=True)
        m.reconcile()
        by = {(s.job_id, s.field_id): s for s in m.status()}
        assert by[("controller", "beta")].state == "idle"
        assert by[("controller", "beta")].next_run_at is not None
    finally:
        m.shutdown()
