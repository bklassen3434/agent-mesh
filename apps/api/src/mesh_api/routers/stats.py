from __future__ import annotations

from fastapi import APIRouter
from mesh_db.beliefs import count_beliefs
from mesh_db.claims import count_claims
from mesh_db.connection import MeshConnection
from mesh_db.entities import count_entities
from mesh_db.pipeline_runs import list_pipeline_runs
from mesh_db.sources import count_sources

from mesh_api.deps import ConnDep
from mesh_api.schemas import StatsResponse

router = APIRouter(prefix="/api/v1", tags=["stats"])


def _scalar_count(conn: MeshConnection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0]) if row else 0


@router.get(
    "/stats",
    response_model=StatsResponse,
    summary="Top-level mesh counts",
    description=(
        "Counts of all entity types plus the most recent pipeline run "
        "timestamp. Used by the wiki home dashboard."
    ),
)
def stats(conn: ConnDep) -> StatsResponse:
    runs = list_pipeline_runs(conn, limit=1)
    last = runs[0] if runs else None
    return StatsResponse(
        entities=count_entities(conn),
        claims=count_claims(conn),
        beliefs=count_beliefs(conn),
        sources=count_sources(conn),
        revisions=_scalar_count(conn, "SELECT COUNT(*) FROM belief_revisions"),
        pipeline_runs=_scalar_count(conn, "SELECT COUNT(*) FROM pipeline_runs"),
        last_pipeline_run_at=(last.finished_at or last.started_at) if last else None,
        last_pipeline_run_id=last.id if last else None,
    )
