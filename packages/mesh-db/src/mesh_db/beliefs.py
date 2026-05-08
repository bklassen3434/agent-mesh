from __future__ import annotations

from datetime import datetime
from typing import Any

import duckdb
from mesh_models.belief import Belief


def _row_to_belief(row: tuple[Any, ...]) -> Belief:
    (
        id_, topic, statement, supporting_claim_ids, contradicting_claim_ids,
        confidence, last_revised_at, revision_count, is_currently_held,
    ) = row[:9]
    return Belief(
        id=id_,
        topic=topic,
        statement=statement,
        supporting_claim_ids=list(supporting_claim_ids) if supporting_claim_ids else [],
        contradicting_claim_ids=list(contradicting_claim_ids) if contradicting_claim_ids else [],
        confidence=float(confidence),
        last_revised_at=(
            last_revised_at if isinstance(last_revised_at, datetime)
            else datetime.fromisoformat(str(last_revised_at))
        ),
        revision_count=int(revision_count),
        is_currently_held=bool(is_currently_held),
    )


_SELECT = (
    "SELECT id, topic, statement, supporting_claim_ids, contradicting_claim_ids, "
    "confidence, last_revised_at, revision_count, is_currently_held FROM beliefs"
)


def create_belief(conn: duckdb.DuckDBPyConnection, model: Belief) -> Belief:
    conn.execute(
        """
        INSERT INTO beliefs (id, topic, statement, supporting_claim_ids, contradicting_claim_ids,
            confidence, last_revised_at, revision_count, is_currently_held)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            model.id,
            model.topic,
            model.statement,
            model.supporting_claim_ids,
            model.contradicting_claim_ids,
            model.confidence,
            model.last_revised_at,
            model.revision_count,
            model.is_currently_held,
        ],
    )
    return model


def get_belief_by_id(conn: duckdb.DuckDBPyConnection, id: str) -> Belief | None:
    row = conn.execute(f"{_SELECT} WHERE id = ?", [id]).fetchone()
    return _row_to_belief(row) if row else None


def list_beliefs(
    conn: duckdb.DuckDBPyConnection,
    topic: str | None = None,
    currently_held: bool | None = None,
    limit: int = 100,
) -> list[Belief]:
    conditions: list[str] = []
    params: list[Any] = []
    if topic is not None:
        conditions.append("topic ILIKE ?")
        params.append(f"%{topic}%")
    if currently_held is not None:
        conditions.append("is_currently_held = ?")
        params.append(currently_held)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    rows = conn.execute(
        f"{_SELECT}{where} ORDER BY last_revised_at DESC LIMIT ?", params
    ).fetchall()
    return [_row_to_belief(r) for r in rows]


def update_belief(
    conn: duckdb.DuckDBPyConnection, id: str, **fields: Any
) -> Belief:
    allowed = {
        "statement", "supporting_claim_ids", "contradicting_claim_ids",
        "confidence", "last_revised_at", "revision_count", "is_currently_held",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        belief = get_belief_by_id(conn, id)
        if belief is None:
            raise ValueError(f"Belief {id} not found")
        return belief

    set_clauses = [f"{k} = ?" for k in updates]
    params: list[Any] = list(updates.values())
    params.append(id)
    conn.execute(
        f"UPDATE beliefs SET {', '.join(set_clauses)} WHERE id = ?", params
    )
    belief = get_belief_by_id(conn, id)
    if belief is None:
        raise ValueError(f"Belief {id} not found after update")
    return belief
