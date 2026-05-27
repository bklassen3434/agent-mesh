"""Phase 6a scheduler unit tests.

Doesn't run the BlockingScheduler — we only verify that job registration
wires the right crons and that ``configured_cron_triggers`` reads env
overrides correctly. The actual job bodies (subprocess invocations of
mesh-pipeline / mesh-skeptic-sweep) are exercised end-to-end via
``make pipeline`` and the 90-minute observation window described in
the phase exit criteria.
"""
from __future__ import annotations

import pytest
from mesh_scheduler import (
    build_scheduler,
    configured_cron_triggers,
    register_jobs,
)


def test_default_triggers_match_env_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MESH_SCHEDULE_PIPELINE_CRON", raising=False)
    monkeypatch.delenv("MESH_SCHEDULE_SWEEP_CRON", raising=False)
    triggers = configured_cron_triggers()
    assert set(triggers.keys()) == {"pipeline", "skeptic_sweep"}
    # Defaults: every 6h on the hour, daily at 03:00.
    pipeline_str = str(triggers["pipeline"])
    sweep_str = str(triggers["skeptic_sweep"])
    assert "hour='*/6'" in pipeline_str
    assert "hour='3'" in sweep_str


def test_env_overrides_picked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_SCHEDULE_PIPELINE_CRON", "*/20 * * * *")
    monkeypatch.setenv("MESH_SCHEDULE_SWEEP_CRON", "*/30 * * * *")
    triggers = configured_cron_triggers()
    assert "minute='*/20'" in str(triggers["pipeline"])
    assert "minute='*/30'" in str(triggers["skeptic_sweep"])


def test_register_jobs_returns_both(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MESH_SCHEDULE_PIPELINE_CRON", raising=False)
    monkeypatch.delenv("MESH_SCHEDULE_SWEEP_CRON", raising=False)
    sched = build_scheduler()
    jobs = register_jobs(
        sched,
        pipeline_runner=lambda: None,
        sweep_runner=lambda: None,
    )
    assert set(jobs.keys()) == {"pipeline", "skeptic_sweep"}
    # Idempotent re-registration (replace_existing=True) is what protects
    # against double-fire if the scheduler is restarted mid-day.
    again = register_jobs(
        sched,
        pipeline_runner=lambda: None,
        sweep_runner=lambda: None,
    )
    assert again["pipeline"].id == "pipeline"
