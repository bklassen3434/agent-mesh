from __future__ import annotations

from typing import Any

import duckdb
from mesh_models.relationship import Relationship


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


def create_relationship(conn: duckdb.DuckDBPyConnection, model: Relationship) -> Relationship:
    conn.execute(
        """
        INSERT INTO relationships
            (id, from_entity_id, to_entity_id, type, evidence_claim_ids, confidence)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            model.id,
            model.from_entity_id,
            model.to_entity_id,
            model.type,
            model.evidence_claim_ids,
            model.confidence,
        ],
    )
    return model


def get_relationship_by_id(conn: duckdb.DuckDBPyConnection, id: str) -> Relationship | None:
    row = conn.execute(f"{_SELECT} WHERE id = ?", [id]).fetchone()
    return _row_to_relationship(row) if row else None


def list_relationships(
    conn: duckdb.DuckDBPyConnection,
    from_entity_id: str | None = None,
    to_entity_id: str | None = None,
    limit: int = 100,
) -> list[Relationship]:
    conditions: list[str] = []
    params: list[Any] = []
    if from_entity_id is not None:
        conditions.append("from_entity_id = ?")
        params.append(from_entity_id)
    if to_entity_id is not None:
        conditions.append("to_entity_id = ?")
        params.append(to_entity_id)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    rows = conn.execute(f"{_SELECT}{where} LIMIT ?", params).fetchall()
    return [_row_to_relationship(r) for r in rows]


def update_relationship(
    conn: duckdb.DuckDBPyConnection, id: str, **fields: Any
) -> Relationship:
    allowed = {"type", "evidence_claim_ids", "confidence"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        rel = get_relationship_by_id(conn, id)
        if rel is None:
            raise ValueError(f"Relationship {id} not found")
        return rel

    set_clauses = [f"{k} = ?" for k in updates]
    params: list[Any] = list(updates.values())
    params.append(id)
    conn.execute(
        f"UPDATE relationships SET {', '.join(set_clauses)} WHERE id = ?", params
    )
    rel = get_relationship_by_id(conn, id)
    if rel is None:
        raise ValueError(f"Relationship {id} not found after update")
    return rel
