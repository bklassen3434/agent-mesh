"""Schedule config endpoints (Phase 9).

Read/write the Postgres ``schedules`` table. The only write endpoints in
the otherwise read-only API. A successful PATCH best-effort signals the
scheduler to apply the change immediately; the scheduler's 30s reconcile
poll is the safety net if that signal is missed.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from mesh_a2a.schedules import (
    DEFAULT_INTERVALS,
    SchedulesUnavailable,
    list_schedules,
    update_schedule,
)
from mesh_models.schedule import ALLOWED_INTERVAL_HOURS, Schedule, ScheduleUpdate

from mesh_api.scheduler_client import signal_reload

router = APIRouter(prefix="/api/v1/schedules", tags=["schedules"])

_KNOWN_JOBS = set(DEFAULT_INTERVALS)


@router.get(
    "",
    response_model=list[Schedule],
    summary="List pipeline schedules",
    description="Current interval + enabled state for each pipeline job.",
)
def list_schedules_endpoint() -> list[Schedule]:
    try:
        return list_schedules()
    except SchedulesUnavailable as exc:
        raise HTTPException(status_code=503, detail="Schedule store not configured") from exc


@router.patch(
    "/{job_id}",
    response_model=Schedule,
    summary="Update a pipeline schedule",
    description=(
        "Patch the interval and/or enabled flag for a job. Persists to "
        "Postgres and signals the scheduler to apply the change without a "
        "restart. ``interval_hours`` must be one of "
        f"{list(ALLOWED_INTERVAL_HOURS)}."
    ),
)
def patch_schedule(
    job_id: str, body: ScheduleUpdate, field: str = Query("ai-robotics")
) -> Schedule:
    if job_id not in _KNOWN_JOBS:
        raise HTTPException(status_code=404, detail=f"Unknown job {job_id}")
    if body.interval_hours is None and body.enabled is None:
        raise HTTPException(
            status_code=422, detail="Provide interval_hours and/or enabled"
        )
    if body.interval_hours is not None and body.interval_hours not in ALLOWED_INTERVAL_HOURS:
        raise HTTPException(
            status_code=422,
            detail=f"interval_hours must be one of {list(ALLOWED_INTERVAL_HOURS)}",
        )
    try:
        updated = update_schedule(
            job_id, field_id=field, interval_hours=body.interval_hours, enabled=body.enabled
        )
    except SchedulesUnavailable as exc:
        raise HTTPException(status_code=503, detail="Schedule store not configured") from exc
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Schedule {job_id} not found")
    signal_reload()
    return updated
