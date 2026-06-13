from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from mesh_db.beliefs import count_beliefs, get_belief_by_id, list_beliefs
from mesh_db.claims import get_claims_by_ids
from mesh_db.connection import MeshConnection
from mesh_db.entities import get_entities_by_ids
from mesh_db.revisions import list_revisions
from mesh_db.sources import get_sources_by_ids
from mesh_models.belief import Belief
from mesh_models.revision import BeliefRevision

from mesh_api.deps import ConnDep
from mesh_api.schemas import (
    BeliefDetail,
    BeliefSignals,
    BeliefSignalSummary,
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
    field: str = Query("ai-robotics", description="Field slug to scope results to"),
) -> Page[Belief]:
    items = list_beliefs(
        conn,
        topic=topic,
        currently_held=currently_held,
        limit=limit,
        offset=offset,
        field_id=field,
    )
    total = count_beliefs(conn, topic=topic, currently_held=currently_held, field_id=field)
    return Page[Belief](items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/signals",
    response_model=list[BeliefSignalSummary],
    summary="Batch belief signal summaries",
    description=(
        "hype/substance score + reproduction count for the given belief ids "
        "(or all currently-held beliefs when none are given). Powers the "
        "inline signal badges on the beliefs list without an N+1 fan-out. "
        "Registered before /{belief_id} so 'signals' isn't read as an id."
    ),
)
def belief_signals_batch(
    conn: ConnDep,
    ids: Annotated[list[str], Query()] = [],  # noqa: B006 — FastAPI query default
) -> list[BeliefSignalSummary]:
    sql = (
        "SELECT belief_id, hype_substance_score, reproduction_count "
        "FROM belief_hype_substance"
    )
    params: list[object] = []
    if ids:
        placeholders = ",".join(["%s"] * len(ids))
        sql += f" WHERE belief_id IN ({placeholders})"
        params.extend(ids)
    rows = conn.execute(sql, params).fetchall()
    return [
        BeliefSignalSummary(
            belief_id=str(r[0]),
            hype_substance_score=float(r[1]),
            reproduction_count=int(r[2]),
        )
        for r in rows
    ]


def _hydrate_claims(
    conn: MeshConnection, claim_ids: list[str]
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


def hydrate_revisions(
    conn: MeshConnection, revisions: list[BeliefRevision]
) -> list[RevisionWithTriggers]:
    """Join a list of revisions with their trigger claims, preserving order."""
    trigger_ids = {cid for r in revisions for cid in r.trigger_claim_ids}
    trigger_claims = {c.id: c for c in get_claims_by_ids(conn, list(trigger_ids))}
    return [
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
    revisions_out = hydrate_revisions(conn, revisions)

    signals = _read_signals(conn, belief_id) if belief.is_currently_held else None

    return BeliefDetail(
        belief=belief,
        supporting_claims=supporting,
        contradicting_claims=contradicting,
        revisions=revisions_out,
        signals=signals,
    )


def _read_signals(
    conn: MeshConnection, belief_id: str
) -> BeliefSignals | None:
    row = conn.execute(
        """
        SELECT source_type_diversity, reproduction_count,
               skeptic_counter_claim_count, severe_failure_mode_count,
               claims_last_30d, hype_substance_score
        FROM belief_hype_substance
        WHERE belief_id = %s
        """,
        [belief_id],
    ).fetchone()
    if row is None:
        return None
    return BeliefSignals(
        source_type_diversity=int(row[0]),
        reproduction_count=int(row[1]),
        skeptic_counter_claim_count=int(row[2]),
        severe_failure_mode_count=int(row[3]),
        claims_last_30d=int(row[4]),
        hype_substance_score=float(row[5]),
    )


@router.get(
    "/{belief_id}/revisions",
    response_model=list[RevisionWithTriggers],
    summary="Revisions for a belief",
    description=(
        "Append-only revision history for a single belief, joined with the "
        "claims that triggered each revision. Ordered most-recent-first."
    ),
)
def belief_revisions(
    belief_id: str,
    conn: ConnDep,
    limit: int = Query(100, ge=1, le=200),
) -> list[RevisionWithTriggers]:
    belief = get_belief_by_id(conn, belief_id)
    if belief is None:
        raise HTTPException(status_code=404, detail="Belief not found")
    revisions = list_revisions(conn, belief_id=belief_id, limit=limit)
    return hydrate_revisions(conn, revisions)
