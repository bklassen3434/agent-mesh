"""Schedule + orchestration-control models (Phase 9).

Transport models for the wiki's Pipelines page. A ``Schedule`` mirrors one
row of the Postgres ``schedules`` table; the others shape the
schedule/trigger/scheduler-status API responses. Kept here (not in the
API layer) so the contract is exported once through the OpenAPI →
TypeScript pipeline the wiki consumes.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

# Intervals the UI offers and the API accepts. Hard-coded set, not free-form.
ALLOWED_INTERVAL_HOURS = (1, 2, 4, 6, 12, 24, 48)


class Schedule(BaseModel):
    """One configured pipeline schedule (a ``schedules`` row)."""

    job_id: str
    field_id: str = "ai-robotics"
    interval_hours: int
    enabled: bool
    updated_at: datetime


class ScheduleUpdate(BaseModel):
    """Partial update for a schedule — interval, enabled, or both."""

    interval_hours: int | None = None
    enabled: bool | None = None


class TriggerResult(BaseModel):
    """Result of an immediate pipeline trigger."""

    run_id: str
    triggered_at: datetime


class SchedulerJobStatus(BaseModel):
    """Live APScheduler state for one job.

    ``state`` is one of ``running`` | ``idle`` | ``disabled``. ``next_run_at``
    is null when the job is paused (disabled) or has no future fire time.
    """

    job_id: str
    field_id: str = "ai-robotics"
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    state: str
