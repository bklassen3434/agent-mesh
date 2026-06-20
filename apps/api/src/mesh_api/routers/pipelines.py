"""Pipeline execution + live scheduler state (Phase 9).

These proxy the scheduler service, which owns execution and the live
APScheduler job state. The API layer adds nothing but validation and
graceful degradation when the scheduler is unreachable.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Query
from mesh_a2a.schedules import DEFAULT_INTERVALS
from mesh_models.schedule import SchedulerJobStatus, TriggerResult

from mesh_api.scheduler_client import fetch_status, trigger_run

router = APIRouter(prefix="/api/v1", tags=["pipelines"])

_KNOWN_JOBS = set(DEFAULT_INTERVALS)


@router.post(
    "/pipelines/{job_id}/trigger",
    response_model=TriggerResult,
    summary="Trigger an immediate pipeline run",
    description=(
        "Starts an out-of-band controller run. "
        "Returns 409 if a run for that job is already in progress."
    ),
)
def trigger_pipeline(
    job_id: str, field: str = Query("ai-robotics")
) -> TriggerResult:
    if job_id not in _KNOWN_JOBS:
        raise HTTPException(status_code=404, detail=f"Unknown job {job_id}")
    try:
        resp = trigger_run(job_id, field)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Scheduler unreachable") from exc
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail="A run is already in progress")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Unknown job {job_id}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Scheduler error")
    data = resp.json()
    return TriggerResult(run_id=data["run_id"], triggered_at=data["triggered_at"])


@router.get(
    "/scheduler/status",
    response_model=list[SchedulerJobStatus],
    summary="Live scheduler job state",
    description=(
        "Per-job next-run, last-run, and state (running / idle / disabled) "
        "from the running scheduler. Returns an empty list if the scheduler "
        "is unreachable so the Pipelines page degrades gracefully."
    ),
)
def scheduler_status() -> list[SchedulerJobStatus]:
    try:
        data = fetch_status()
    except httpx.HTTPError:
        return []
    return [SchedulerJobStatus(**d) for d in data]
