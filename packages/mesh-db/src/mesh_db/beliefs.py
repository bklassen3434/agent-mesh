from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


MAX_LIMIT = 200


def _belief_filters(
    topic: str | None, currently_held: bool | None
) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if topic is not None:
        conditions.append("topic ILIKE ?")
        params.append(f"%{topic}%")
    if currently_held is not None:
        conditions.append("is_currently_held = ?")
        params.append(currently_held)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return where, params


def list_beliefs(
    conn: duckdb.DuckDBPyConnection,
    topic: str | None = None,
    currently_held: bool | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Belief]:
    limit = min(max(limit, 0), MAX_LIMIT)
    offset = max(offset, 0)
    where, params = _belief_filters(topic, currently_held)
    params.extend([limit, offset])
    rows = conn.execute(
        f"{_SELECT}{where} ORDER BY last_revised_at DESC LIMIT ? OFFSET ?", params
    ).fetchall()
    return [_row_to_belief(r) for r in rows]


def count_beliefs(
    conn: duckdb.DuckDBPyConnection,
    topic: str | None = None,
    currently_held: bool | None = None,
) -> int:
    where, params = _belief_filters(topic, currently_held)
    row = conn.execute(f"SELECT COUNT(*) FROM beliefs{where}", params).fetchone()
    return int(row[0]) if row else 0


def find_stale_beliefs(
    conn: duckdb.DuckDBPyConnection,
    threshold_days: int,
    limit: int = 100,
) -> list[Belief]:
    """Beliefs whose most recent supporting/contradicting claim is older than ``threshold_days``.

    A belief with no claims attached is treated as stale (no fresh evidence).
    Ordered by the oldest most-recent-claim first so callers (e.g. Curator)
    can prioritize the staler ones. Currently-held beliefs only — superseded
    beliefs don't need re-evaluation.
    """
    cutoff = datetime.now(UTC) - timedelta(days=threshold_days)
    limit = min(max(limit, 0), MAX_LIMIT)
    # Join via UNNEST on each claim-id array, MAX the extracted_at across both
    # to get the most recent evidence timestamp per belief. COALESCE so the
    # no-claims case sorts oldest first via a far-past sentinel.
    rows = conn.execute(
        """
        WITH belief_claim_links AS (
            SELECT id AS belief_id,
                   UNNEST(supporting_claim_ids) AS claim_id
            FROM beliefs WHERE is_currently_held = TRUE
            UNION ALL
            SELECT id AS belief_id,
                   UNNEST(contradicting_claim_ids) AS claim_id
            FROM beliefs WHERE is_currently_held = TRUE
        ),
        belief_evidence AS (
            SELECT b.id AS belief_id,
                   MAX(c.extracted_at) AS last_claim_at
            FROM beliefs b
            LEFT JOIN belief_claim_links bcl ON bcl.belief_id = b.id
            LEFT JOIN claims c ON c.id = bcl.claim_id
            WHERE b.is_currently_held = TRUE
            GROUP BY b.id
        )
        SELECT b.id, b.topic, b.statement, b.supporting_claim_ids,
               b.contradicting_claim_ids, b.confidence, b.last_revised_at,
               b.revision_count, b.is_currently_held
        FROM beliefs b
        JOIN belief_evidence be ON be.belief_id = b.id
        WHERE COALESCE(be.last_claim_at, TIMESTAMPTZ '1970-01-01') < ?
        ORDER BY be.last_claim_at ASC NULLS FIRST
        LIMIT ?
        """,
        [cutoff, limit],
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
