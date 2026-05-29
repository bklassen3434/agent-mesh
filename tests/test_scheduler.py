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
    pipeline_hours: int = 6,
    sweep_hours: int = 24,
    pipeline_enabled: bool = True,
    sweep_enabled: bool = True,
) -> list[Schedule]:
    now = datetime.now(UTC)
    return [
        Schedule(job_id="pipeline", interval_hours=pipeline_hours,
                 enabled=pipeline_enabled, updated_at=now),
        Schedule(job_id="skeptic_sweep", interval_hours=sweep_hours,
                 enabled=sweep_enabled, updated_at=now),
    ]


# ── legacy cron helper (for /status) ─────────────────────────────────────────


def test_default_cron_triggers_match_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MESH_SCHEDULE_PIPELINE_CRON", raising=False)
    monkeypatch.delenv("MESH_SCHEDULE_SWEEP_CRON", raising=False)
    triggers = configured_cron_triggers()
    assert set(triggers.keys()) == {"pipeline", "skeptic_sweep"}
    assert "hour='*/6'" in str(triggers["pipeline"])
    assert "hour='3'" in str(triggers["skeptic_sweep"])


def test_cron_env_overrides_picked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_SCHEDULE_PIPELINE_CRON", "*/20 * * * *")
    triggers = configured_cron_triggers()
    assert "minute='*/20'" in str(triggers["pipeline"])


# ── SchedulerManager — run claiming + status ─────────────────────────────────


def test_begin_is_single_flight() -> None:
    m = SchedulerManager()
    m._state["pipeline"] = sched_mod._JobState(interval_hours=6, enabled=True)
    first = m._begin("pipeline", "manual")
    assert first is not None
    # A second claim while the first run is in flight is refused.
    assert m._begin("pipeline", "manual") is None


def test_status_maps_states() -> None:
    m = SchedulerManager()
    m._state["pipeline"] = sched_mod._JobState(interval_hours=6, enabled=True, running=True)
    m._state["skeptic_sweep"] = sched_mod._JobState(interval_hours=24, enabled=False)
    by_id = {s.job_id: s for s in m.status()}
    assert by_id["pipeline"].state == "running"
    assert by_id["skeptic_sweep"].state == "disabled"


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
        assert m._state["pipeline"].interval_hours == 6
        # Change pipeline interval, disable the sweep.
        schedules[:] = _schedules(pipeline_hours=12, sweep_enabled=False)
        m.reconcile()

        by_id = {s.job_id: s for s in m.status()}
        assert m._state["pipeline"].interval_hours == 12
        assert by_id["skeptic_sweep"].state == "disabled"
        # A paused (disabled) job has no scheduled next fire.
        assert by_id["skeptic_sweep"].next_run_at is None
        # Re-enable and confirm it resumes.
        schedules[:] = _schedules(pipeline_hours=12, sweep_enabled=True)
        m.reconcile()
        by_id = {s.job_id: s for s in m.status()}
        assert by_id["skeptic_sweep"].state == "idle"
        assert by_id["skeptic_sweep"].next_run_at is not None
    finally:
        m.shutdown()
