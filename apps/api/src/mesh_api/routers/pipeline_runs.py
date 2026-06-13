from __future__ import annotations

from fastapi import APIRouter, Query
from mesh_db.pipeline_runs import PipelineRun, list_pipeline_runs

from mesh_api.deps import ConnDep

router = APIRouter(prefix="/api/v1", tags=["pipeline-runs"])


@router.get(
    "/pipeline-runs",
    response_model=list[PipelineRun],
    summary="Recent pipeline runs",
    description="Most recent pipeline runs, newest first. Defaults to 10.",
)
def list_runs(
    conn: ConnDep,
    limit: int = Query(10, ge=1, le=200),
    field: str = Query("ai-robotics", description="Field slug to scope results to"),
) -> list[PipelineRun]:
    return list_pipeline_runs(conn, limit=limit, field_id=field)
