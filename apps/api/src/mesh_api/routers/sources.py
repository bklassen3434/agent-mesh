from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from mesh_db.claims import count_claims, list_claims
from mesh_db.sources import count_sources, get_source_by_id, list_sources
from mesh_models.source import SourceType

from mesh_api.deps import ConnDep
from mesh_api.schemas import Page, SourceDetail, SourceWithCount

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])


@router.get(
    "",
    response_model=Page[SourceWithCount],
    summary="List sources with claim counts",
    description=(
        "Paginated source list. Each row includes the number of claims "
        "extracted from that source — useful for ranking and the wiki home page."
    ),
)
def list_sources_endpoint(
    conn: ConnDep,
    type: SourceType | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[SourceWithCount]:
    sources = list_sources(conn, type=type, limit=limit, offset=offset)
    enriched = [
        SourceWithCount(source=s, claim_count=count_claims(conn, source_id=s.id))
        for s in sources
    ]
    total = count_sources(conn, type=type)
    return Page[SourceWithCount](
        items=enriched, total=total, limit=limit, offset=offset
    )


@router.get(
    "/{source_id}",
    response_model=SourceDetail,
    summary="Source detail",
    description="Source record plus claims extracted from it (newest first).",
)
def source_detail(source_id: str, conn: ConnDep) -> SourceDetail:
    source = get_source_by_id(conn, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    claims = list_claims(conn, source_id=source_id, limit=200)
    return SourceDetail(source=source, claims=claims)
