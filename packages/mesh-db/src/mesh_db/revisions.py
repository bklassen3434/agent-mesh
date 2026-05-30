from __future__ import annotations

from datetime import datetime
from typing import Any

from mesh_models.revision import BeliefRevision

from mesh_db.connection import MeshConnection


def _row_to_revision(row: tuple[Any, ...]) -> BeliefRevision:
    (
        id_, belief_id, previous_statement, new_statement,
        previous_confidence, new_confidence, trigger_claim_ids,
        revised_by_agent, revised_at, rationale,
    ) = row[:10]
    return BeliefRevision(
        id=id_,
        belief_id=belief_id,
        previous_statement=previous_statement,
        new_statement=new_statement,
        previous_confidence=float(previous_confidence),
        new_confidence=float(new_confidence),
        trigger_claim_ids=list(trigger_claim_ids) if trigger_claim_ids else [],
        revised_by_agent=revised_by_agent,
        revised_at=(
            revised_at if isinstance(revised_at, datetime)
            else datetime.fromisoformat(str(revised_at))
        ),
        rationale=rationale,
    )


_SELECT = (
    "SELECT id, belief_id, previous_statement, new_statement, previous_confidence, "
    "new_confidence, trigger_claim_ids, revised_by_agent, revised_at, rationale "
    "FROM belief_revisions"
)


def create_revision(conn: MeshConnection, model: BeliefRevision) -> BeliefRevision:
    conn.execute(
        """
        INSERT INTO belief_revisions
            (id, belief_id, previous_statement, new_statement,
            previous_confidence, new_confidence, trigger_claim_ids,
            revised_by_agent, revised_at, rationale)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            model.id,
            model.belief_id,
            model.previous_statement,
            model.new_statement,
            model.previous_confidence,
            model.new_confidence,
            model.trigger_claim_ids,
            model.revised_by_agent,
            model.revised_at,
            model.rationale,
        ],
    )
    return model


def get_revision_by_id(conn: MeshConnection, id: str) -> BeliefRevision | None:
    row = conn.execute(f"{_SELECT} WHERE id = %s", [id]).fetchone()
    return _row_to_revision(row) if row else None


MAX_LIMIT = 200


def list_revisions(
    conn: MeshConnection,
    belief_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[BeliefRevision]:
    limit = min(max(limit, 0), MAX_LIMIT)
    offset = max(offset, 0)
    params: list[Any] = []
    where = ""
    if belief_id is not None:
        where = " WHERE belief_id = %s"
        params.append(belief_id)
    params.extend([limit, offset])
    rows = conn.execute(
        f"{_SELECT}{where} ORDER BY revised_at DESC LIMIT %s OFFSET %s", params
    ).fetchall()
    return [_row_to_revision(r) for r in rows]
