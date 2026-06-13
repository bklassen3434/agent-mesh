from __future__ import annotations

from fastapi import APIRouter, Query
from mesh_db.beliefs import get_belief_by_id
from mesh_db.claims import get_claims_by_ids
from mesh_db.revisions import list_revisions

from mesh_api.deps import ConnDep
from mesh_api.schemas import SkepticActivityItem

router = APIRouter(prefix="/api/v1/skeptic", tags=["skeptic"])


@router.get(
    "/recent",
    response_model=list[SkepticActivityItem],
    summary="Recent skeptic-triggered belief revisions",
    description=(
        "Most recent BeliefRevisions where revised_by_agent='skeptic', joined "
        "with each revision's belief and trigger claims (the counter-claims "
        "the skeptic emitted). Drives the wiki's skeptic-activity feed."
    ),
)
def recent_skeptic_activity(
    conn: ConnDep,
    limit: int = Query(20, ge=1, le=100),
    field: str = Query("ai-robotics", description="Field slug to scope results to"),
) -> list[SkepticActivityItem]:
    # Scoped to the field via list_revisions' field_id filter (EXISTS over
    # beliefs). list_revisions has no agent filter; pull a generous window and
    # filter to skeptic-authored rows in Python — the skeptic-revision rate is
    # tiny relative to the table so this is fine for now.
    candidates = list_revisions(conn, limit=200, field_id=field)
    skeptic_revs = [r for r in candidates if r.revised_by_agent == "skeptic"][:limit]

    if not skeptic_revs:
        return []

    # Batch hydrate beliefs + trigger claims to avoid N+1 queries.
    belief_ids = list({r.belief_id for r in skeptic_revs})
    beliefs = {
        b.id: b
        for b in (get_belief_by_id(conn, bid) for bid in belief_ids)
        if b is not None
    }

    trigger_ids = list({cid for r in skeptic_revs for cid in r.trigger_claim_ids})
    trigger_claims = {c.id: c for c in get_claims_by_ids(conn, trigger_ids)}

    out: list[SkepticActivityItem] = []
    for r in skeptic_revs:
        belief = beliefs.get(r.belief_id)
        if belief is None:
            # Defensive — a belief shouldn't be missing for its own revision,
            # but skip rather than 500 if the DB is inconsistent.
            continue
        out.append(
            SkepticActivityItem(
                revision=r,
                belief=belief,
                trigger_claims=[
                    trigger_claims[cid]
                    for cid in r.trigger_claim_ids
                    if cid in trigger_claims
                ],
            )
        )
    return out
