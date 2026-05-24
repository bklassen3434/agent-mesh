from __future__ import annotations

import duckdb
from fastapi import APIRouter, HTTPException, Query
from mesh_db.beliefs import count_beliefs, get_belief_by_id, list_beliefs
from mesh_db.claims import get_claims_by_ids
from mesh_db.entities import get_entities_by_ids
from mesh_db.revisions import list_revisions
from mesh_db.sources import get_sources_by_ids
from mesh_models.belief import Belief

from mesh_api.deps import ConnDep
from mesh_api.schemas import (
    BeliefDetail,
    ClaimWithContext,
    Page,
    RevisionWithTriggers,
)

router = APIRouter(prefix="/api/v1/beliefs", tags=["beliefs"])


@router.get(
    "",
    response_model=Page[Belief],
    summary="List beliefs",
    description=(
        "Paginated belief list, ordered by most recently revised. Optional "
        "topic substring filter and currently_held boolean."
    ),
)
def list_beliefs_endpoint(
    conn: ConnDep,
    topic: str | None = None,
    currently_held: bool | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[Belief]:
    items = list_beliefs(
        conn,
        topic=topic,
        currently_held=currently_held,
        limit=limit,
        offset=offset,
    )
    total = count_beliefs(conn, topic=topic, currently_held=currently_held)
    return Page[Belief](items=items, total=total, limit=limit, offset=offset)


def _hydrate_claims(
    conn: duckdb.DuckDBPyConnection, claim_ids: list[str]
) -> list[ClaimWithContext]:
    if not claim_ids:
        return []
    claims = get_claims_by_ids(conn, claim_ids)
    by_id = {c.id: c for c in claims}
    ordered = [by_id[cid] for cid in claim_ids if cid in by_id]
    entity_ids = list({c.subject_entity_id for c in ordered})
    source_ids = list({c.source_id for c in ordered})
    entities = {e.id: e for e in get_entities_by_ids(conn, entity_ids)}
    sources = {s.id: s for s in get_sources_by_ids(conn, source_ids)}
    return [
        ClaimWithContext(
            claim=c,
            source=sources.get(c.source_id),
            subject_entity=entities.get(c.subject_entity_id),
        )
        for c in ordered
    ]


@router.get(
    "/{belief_id}",
    response_model=BeliefDetail,
    summary="Belief detail with full provenance",
    description=(
        "Belief plus its supporting and contradicting claims (each joined with "
        "source and subject entity) and the full revision history. Each "
        "revision lists the claims that triggered it. This is the "
        "screenshot-worthy view: claims immutable, beliefs mutable, full "
        "provenance navigable."
    ),
)
def belief_detail(belief_id: str, conn: ConnDep) -> BeliefDetail:
    belief = get_belief_by_id(conn, belief_id)
    if belief is None:
        raise HTTPException(status_code=404, detail="Belief not found")

    supporting = _hydrate_claims(conn, belief.supporting_claim_ids)
    contradicting = _hydrate_claims(conn, belief.contradicting_claim_ids)

    revisions = list_revisions(conn, belief_id=belief_id, limit=200)
    trigger_ids = {cid for r in revisions for cid in r.trigger_claim_ids}
    trigger_claims = {c.id: c for c in get_claims_by_ids(conn, list(trigger_ids))}
    revisions_out = [
        RevisionWithTriggers(
            revision=r,
            trigger_claims=[
                trigger_claims[cid]
                for cid in r.trigger_claim_ids
                if cid in trigger_claims
            ],
        )
        for r in revisions
    ]

    return BeliefDetail(
        belief=belief,
        supporting_claims=supporting,
        contradicting_claims=contradicting,
        revisions=revisions_out,
    )
