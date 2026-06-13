from __future__ import annotations

from typing import Any

from mesh_models.field import DEFAULT_FIELD_ID
from mesh_models.relationship import Relationship

from mesh_db.connection import MeshConnection


def _row_to_relationship(row: tuple[Any, ...]) -> Relationship:
    id_, from_entity_id, to_entity_id, type_, evidence_claim_ids, confidence = row[:6]
    return Relationship(
        id=id_,
        from_entity_id=from_entity_id,
        to_entity_id=to_entity_id,
        type=type_,
        evidence_claim_ids=list(evidence_claim_ids) if evidence_claim_ids else [],
        confidence=float(confidence),
    )


_SELECT = (
    "SELECT id, from_entity_id, to_entity_id, type, evidence_claim_ids, confidence "
    "FROM relationships"
)


def create_relationship(
    conn: MeshConnection, model: Relationship, *, field_id: str = DEFAULT_FIELD_ID
) -> Relationship:
    conn.execute(
        """
        INSERT INTO relationships
            (id, field_id, from_entity_id, to_entity_id, type, evidence_claim_ids,
             confidence)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        [
            model.id,
            field_id,
            model.from_entity_id,
            model.to_entity_id,
            model.type,
            model.evidence_claim_ids,
            model.confidence,
        ],
    )
    return model


def get_relationship_by_id(conn: MeshConnection, id: str) -> Relationship | None:
    row = conn.execute(f"{_SELECT} WHERE id = %s", [id]).fetchone()
    return _row_to_relationship(row) if row else None


def list_relationships(
    conn: MeshConnection,
    from_entity_id: str | None = None,
    to_entity_id: str | None = None,
    limit: int = 100,
    field_id: str | None = None,
) -> list[Relationship]:
    conditions: list[str] = []
    params: list[Any] = []
    if field_id is not None:
        conditions.append("field_id = %s")
        params.append(field_id)
    if from_entity_id is not None:
        conditions.append("from_entity_id = %s")
        params.append(from_entity_id)
    if to_entity_id is not None:
        conditions.append("to_entity_id = %s")
        params.append(to_entity_id)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    rows = conn.execute(f"{_SELECT}{where} LIMIT %s", params).fetchall()
    return [_row_to_relationship(r) for r in rows]


def find_relationship(
    conn: MeshConnection,
    from_entity_id: str,
    to_entity_id: str,
    type: str,
    field_id: str | None = None,
) -> Relationship | None:
    """Locate the single edge for a (from, to, type) triple, if it exists."""
    conditions = ["from_entity_id = %s", "to_entity_id = %s", "type = %s"]
    params: list[Any] = [from_entity_id, to_entity_id, type]
    if field_id is not None:
        conditions.insert(0, "field_id = %s")
        params.insert(0, field_id)
    row = conn.execute(
        f"{_SELECT} WHERE {' AND '.join(conditions)} LIMIT 1",
        params,
    ).fetchone()
    return _row_to_relationship(row) if row else None


def add_relationship_evidence(
    conn: MeshConnection,
    from_entity_id: str,
    to_entity_id: str,
    type: str,
    claim_id: str,
    confidence: float,
    *,
    field_id: str = DEFAULT_FIELD_ID,
) -> tuple[Relationship, bool]:
    """Claim-grounded edge upsert (Phase 14c). Aggregates onto one edge per
    (from, to, type): creates it if absent, else appends the evidence claim
    (deduped) and lifts confidence to the strongest supporting claim. Idempotent.

    Returns ``(relationship, created)`` where ``created`` is True only when a new
    edge row was inserted."""
    existing = find_relationship(
        conn, from_entity_id, to_entity_id, type, field_id=field_id
    )
    if existing is None:
        rel = Relationship(
            from_entity_id=from_entity_id,
            to_entity_id=to_entity_id,
            type=type,
            evidence_claim_ids=[claim_id],
            confidence=confidence,
        )
        create_relationship(conn, rel, field_id=field_id)
        return rel, True
    if claim_id in existing.evidence_claim_ids:
        return existing, False
    updated = update_relationship(
        conn,
        existing.id,
        evidence_claim_ids=[*existing.evidence_claim_ids, claim_id],
        confidence=max(existing.confidence, confidence),
    )
    return updated, False


def update_relationship(
    conn: MeshConnection, id: str, **fields: Any
) -> Relationship:
    allowed = {"type", "evidence_claim_ids", "confidence"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        rel = get_relationship_by_id(conn, id)
        if rel is None:
            raise ValueError(f"Relationship {id} not found")
        return rel

    set_clauses = [f"{k} = %s" for k in updates]
    params: list[Any] = list(updates.values())
    params.append(id)
    conn.execute(
        f"UPDATE relationships SET {', '.join(set_clauses)} WHERE id = %s", params
    )
    rel = get_relationship_by_id(conn, id)
    if rel is None:
        raise ValueError(f"Relationship {id} not found after update")
    return rel
