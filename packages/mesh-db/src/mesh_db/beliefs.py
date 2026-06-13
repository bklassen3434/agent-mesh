from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from mesh_models.belief import Belief
from mesh_models.field import DEFAULT_FIELD_ID

from mesh_db.connection import MeshConnection


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


def create_belief(
    conn: MeshConnection, model: Belief, *, field_id: str = DEFAULT_FIELD_ID
) -> Belief:
    conn.execute(
        """
        INSERT INTO beliefs (id, field_id, topic, statement, supporting_claim_ids,
            contradicting_claim_ids, confidence, last_revised_at, revision_count,
            is_currently_held)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            model.id,
            field_id,
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


def get_belief_by_id(conn: MeshConnection, id: str) -> Belief | None:
    row = conn.execute(f"{_SELECT} WHERE id = %s", [id]).fetchone()
    return _row_to_belief(row) if row else None


MAX_LIMIT = 200


def _belief_filters(
    topic: str | None, currently_held: bool | None, field_id: str | None = None
) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if field_id is not None:
        conditions.append("field_id = %s")
        params.append(field_id)
    if topic is not None:
        conditions.append("topic ILIKE %s")
        params.append(f"%{topic}%")
    if currently_held is not None:
        conditions.append("is_currently_held = %s")
        params.append(currently_held)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return where, params


def list_beliefs(
    conn: MeshConnection,
    topic: str | None = None,
    currently_held: bool | None = None,
    limit: int = 100,
    offset: int = 0,
    field_id: str | None = None,
) -> list[Belief]:
    limit = min(max(limit, 0), MAX_LIMIT)
    offset = max(offset, 0)
    where, params = _belief_filters(topic, currently_held, field_id)
    params.extend([limit, offset])
    rows = conn.execute(
        f"{_SELECT}{where} ORDER BY last_revised_at DESC LIMIT %s OFFSET %s", params
    ).fetchall()
    return [_row_to_belief(r) for r in rows]


def count_beliefs(
    conn: MeshConnection,
    topic: str | None = None,
    currently_held: bool | None = None,
    field_id: str | None = None,
) -> int:
    where, params = _belief_filters(topic, currently_held, field_id)
    row = conn.execute(f"SELECT COUNT(*) FROM beliefs{where}", params).fetchone()
    return int(row[0]) if row else 0


def find_stale_beliefs(
    conn: MeshConnection,
    threshold_days: int,
    limit: int = 100,
    field_id: str | None = None,
) -> list[Belief]:
    """Beliefs whose most recent supporting/contradicting claim is older than ``threshold_days``.

    A belief with no claims attached is treated as stale (no fresh evidence).
    Ordered by the oldest most-recent-claim first so callers (e.g. Curator)
    can prioritize the staler ones. Currently-held beliefs only — superseded
    beliefs don't need re-evaluation.
    """
    cutoff = datetime.now(UTC) - timedelta(days=threshold_days)
    limit = min(max(limit, 0), MAX_LIMIT)
    field_condition = "b.field_id = %s AND " if field_id is not None else ""
    params: list[Any] = [field_id] if field_id is not None else []
    params.extend([cutoff, limit])
    # Join via UNNEST on each claim-id array, MAX the extracted_at across both
    # to get the most recent evidence timestamp per belief. COALESCE so the
    # no-claims case sorts oldest first via a far-past sentinel.
    rows = conn.execute(
        f"""
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
        WHERE {field_condition}COALESCE(be.last_claim_at, TIMESTAMPTZ '1970-01-01') < %s
        ORDER BY be.last_claim_at ASC NULLS FIRST
        LIMIT %s
        """,
        params,
    ).fetchall()
    return [_row_to_belief(r) for r in rows]


def get_belief_signals(conn: MeshConnection, belief_id: str) -> dict[str, int]:
    """Read a belief's evidence signals from the belief_signals view (Phase 14d).

    Returns all-zero signals for a belief the view doesn't cover (e.g. not
    currently held). The view recomputes on read, so it reflects a belief's
    claim links as soon as they're written."""
    row = conn.execute(
        """
        SELECT source_type_diversity, reproduction_count,
               skeptic_counter_claim_count, severe_failure_mode_count,
               claims_last_30d
        FROM belief_signals WHERE belief_id = %s
        """,
        [belief_id],
    ).fetchone()
    if row is None:
        return {
            "source_type_diversity": 0,
            "reproduction_count": 0,
            "skeptic_counter_claim_count": 0,
            "severe_failure_mode_count": 0,
            "claims_last_30d": 0,
        }
    return {
        "source_type_diversity": int(row[0]),
        "reproduction_count": int(row[1]),
        "skeptic_counter_claim_count": int(row[2]),
        "severe_failure_mode_count": int(row[3]),
        "claims_last_30d": int(row[4]),
    }


def update_belief(
    conn: MeshConnection, id: str, **fields: Any
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

    set_clauses = [f"{k} = %s" for k in updates]
    params: list[Any] = list(updates.values())
    params.append(id)
    conn.execute(
        f"UPDATE beliefs SET {', '.join(set_clauses)} WHERE id = %s", params
    )
    belief = get_belief_by_id(conn, id)
    if belief is None:
        raise ValueError(f"Belief {id} not found after update")
    return belief
