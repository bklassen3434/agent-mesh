from __future__ import annotations

from fastapi import APIRouter, Query
from mesh_db.beliefs import count_beliefs
from mesh_db.claims import count_claims
from mesh_db.connection import MeshConnection
from mesh_db.entities import count_entities
from mesh_db.pipeline_runs import list_pipeline_runs
from mesh_db.sources import count_sources

from mesh_api.deps import ConnDep
from mesh_api.schemas import StatsResponse

router = APIRouter(prefix="/api/v1", tags=["stats"])


def _scalar_count(
    conn: MeshConnection, sql: str, params: list[object] | None = None
) -> int:
    row = conn.execute(sql, params or []).fetchone()
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
def stats(
    conn: ConnDep,
    field: str = Query("ai-robotics", description="Field slug to scope results to"),
) -> StatsResponse:
    runs = list_pipeline_runs(conn, limit=1, field_id=field)
    last = runs[0] if runs else None
    return StatsResponse(
        entities=count_entities(conn, field_id=field),
        claims=count_claims(conn, field_id=field),
        beliefs=count_beliefs(conn, field_id=field),
        sources=count_sources(conn, field_id=field),
        # belief_revisions has no field_id column (revisions inherit field via
        # their belief); scoping it would need a join, so leave this raw count
        # unscoped for now.
        revisions=_scalar_count(conn, "SELECT COUNT(*) FROM belief_revisions"),
        pipeline_runs=_scalar_count(
            conn, "SELECT COUNT(*) FROM pipeline_runs WHERE field_id = %s", [field]
        ),
        last_pipeline_run_at=(last.finished_at or last.started_at) if last else None,
        last_pipeline_run_id=last.id if last else None,
    )
