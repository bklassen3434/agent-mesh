from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from mesh_db.claims import count_claims, get_claim_by_id, list_claims
from mesh_db.entities import get_entity_by_id
from mesh_db.sources import get_source_by_id
from mesh_models.claim import Claim, ClaimStatus

from mesh_api.deps import ConnDep
from mesh_api.schemas import ClaimDetail, Page

router = APIRouter(prefix="/api/v1/claims", tags=["claims"])


@router.get(
    "",
    response_model=Page[Claim],
    summary="List claims",
    description=(
        "Paginated claim list with filters: predicate, source_id, entity_id "
        "(subject), status. Ordered by extraction time, newest first."
    ),
)
def list_claims_endpoint(
    conn: ConnDep,
    predicate: str | None = None,
    source_id: str | None = None,
    entity_id: str | None = None,
    status: ClaimStatus | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    field: str = Query("ai-robotics", description="Field slug to scope results to"),
) -> Page[Claim]:
    items = list_claims(
        conn,
        entity_id=entity_id,
        source_id=source_id,
        status=status,
        predicate=predicate,
        limit=limit,
        offset=offset,
        field_id=field,
    )
    total = count_claims(
        conn,
        entity_id=entity_id,
        source_id=source_id,
        status=status,
        predicate=predicate,
        field_id=field,
    )
    return Page[Claim](items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/{claim_id}",
    response_model=ClaimDetail,
    summary="Claim detail",
    description="Claim joined with its source and subject entity for display.",
)
def claim_detail(claim_id: str, conn: ConnDep) -> ClaimDetail:
    claim = get_claim_by_id(conn, claim_id)
    if claim is None:
        raise HTTPException(status_code=404, detail="Claim not found")
    source = get_source_by_id(conn, claim.source_id)
    subject = get_entity_by_id(conn, claim.subject_entity_id)
    return ClaimDetail(claim=claim, source=source, subject_entity=subject)
