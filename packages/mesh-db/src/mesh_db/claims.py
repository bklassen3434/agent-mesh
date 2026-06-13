from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from mesh_models.claim import Claim, ClaimStatus, ClaimType, FailureMode
from mesh_models.field import DEFAULT_FIELD_ID
from psycopg.types.json import Jsonb

from mesh_db.connection import MeshConnection


def _row_to_claim(row: tuple[Any, ...]) -> Claim:
    (
        id_, predicate, claim_type, subject_entity_id, object_, source_id,
        extracted_at, extracted_by_agent, raw_excerpt, status,
        confidence, superseded_by_claim_id, failure_mode,
    ) = row[:13]
    return Claim(
        id=id_,
        predicate=predicate,
        claim_type=ClaimType(claim_type),
        subject_entity_id=subject_entity_id,
        object=json.loads(object_) if isinstance(object_, str) else (object_ or {}),
        source_id=source_id,
        extracted_at=(
            extracted_at if isinstance(extracted_at, datetime)
            else datetime.fromisoformat(str(extracted_at))
        ),
        extracted_by_agent=extracted_by_agent,
        raw_excerpt=raw_excerpt,
        status=ClaimStatus(status),
        confidence=float(confidence),
        superseded_by_claim_id=superseded_by_claim_id,
        failure_mode=FailureMode(failure_mode) if failure_mode else None,
    )


_SELECT = (
    "SELECT id, predicate, claim_type, subject_entity_id, object, source_id, "
    "extracted_at, extracted_by_agent, raw_excerpt, status, confidence, "
    "superseded_by_claim_id, failure_mode "
    "FROM claims"
)


def create_claim(
    conn: MeshConnection, model: Claim, *, field_id: str = DEFAULT_FIELD_ID
) -> Claim:
    conn.execute(
        """
        INSERT INTO claims
            (id, field_id, predicate, claim_type, subject_entity_id, object, source_id,
            extracted_at, extracted_by_agent, raw_excerpt, status, confidence,
            superseded_by_claim_id, failure_mode)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            model.id,
            field_id,
            model.predicate,
            model.claim_type.value,
            model.subject_entity_id,
            Jsonb(model.object),
            model.source_id,
            model.extracted_at,
            model.extracted_by_agent,
            model.raw_excerpt,
            model.status.value,
            model.confidence,
            model.superseded_by_claim_id,
            model.failure_mode.value if model.failure_mode else None,
        ],
    )
    return model


def get_claim_by_id(conn: MeshConnection, id: str) -> Claim | None:
    row = conn.execute(f"{_SELECT} WHERE id = %s", [id]).fetchone()
    return _row_to_claim(row) if row else None


MAX_LIMIT = 200


def _claim_filters(
    entity_id: str | None,
    source_id: str | None,
    status: ClaimStatus | None,
    predicate: str | None,
    claim_type: ClaimType | None,
    field_id: str | None = None,
) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if field_id is not None:
        conditions.append("field_id = %s")
        params.append(field_id)
    if entity_id is not None:
        conditions.append("subject_entity_id = %s")
        params.append(entity_id)
    if source_id is not None:
        conditions.append("source_id = %s")
        params.append(source_id)
    if status is not None:
        conditions.append("status = %s")
        params.append(status.value)
    if predicate is not None:
        conditions.append("predicate = %s")
        params.append(predicate)
    if claim_type is not None:
        conditions.append("claim_type = %s")
        params.append(claim_type.value)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return where, params


def list_claims(
    conn: MeshConnection,
    entity_id: str | None = None,
    source_id: str | None = None,
    status: ClaimStatus | None = None,
    predicate: str | None = None,
    claim_type: ClaimType | None = None,
    limit: int = 100,
    offset: int = 0,
    field_id: str | None = None,
) -> list[Claim]:
    limit = min(max(limit, 0), MAX_LIMIT)
    offset = max(offset, 0)
    where, params = _claim_filters(
        entity_id, source_id, status, predicate, claim_type, field_id
    )
    params.extend([limit, offset])
    rows = conn.execute(
        f"{_SELECT}{where} ORDER BY extracted_at DESC LIMIT %s OFFSET %s", params
    ).fetchall()
    return [_row_to_claim(r) for r in rows]


def count_claims(
    conn: MeshConnection,
    entity_id: str | None = None,
    source_id: str | None = None,
    status: ClaimStatus | None = None,
    predicate: str | None = None,
    claim_type: ClaimType | None = None,
    field_id: str | None = None,
) -> int:
    where, params = _claim_filters(
        entity_id, source_id, status, predicate, claim_type, field_id
    )
    row = conn.execute(f"SELECT COUNT(*) FROM claims{where}", params).fetchone()
    return int(row[0]) if row else 0


def recent_claim_counts_by_entity(
    conn: MeshConnection,
    *,
    field_id: str = DEFAULT_FIELD_ID,
    since_days: int = 30,
    limit: int = 20,
) -> list[tuple[str, int]]:
    """Claim velocity per entity over a recent window (Phase 22c trend signal).

    Returns ``(subject_entity_id, claim_count)`` for claims extracted in the
    last ``since_days``, busiest entity first — a rising-topic signal the mesh
    may be under-sampling relative to the attention it's drawing. One aggregate,
    field-scoped."""
    rows = conn.execute(
        """
        SELECT subject_entity_id, COUNT(*) AS claim_count
        FROM claims
        WHERE field_id = %s
          AND extracted_at > (now() - make_interval(days => %s))
        GROUP BY subject_entity_id
        ORDER BY claim_count DESC
        LIMIT %s
        """,
        [field_id, max(int(since_days), 0), max(int(limit), 0)],
    ).fetchall()
    return [(str(r[0]), int(r[1])) for r in rows]


def get_claims_by_ids(
    conn: MeshConnection, ids: list[str]
) -> list[Claim]:
    if not ids:
        return []
    placeholders = ",".join(["%s"] * len(ids))
    rows = conn.execute(
        f"{_SELECT} WHERE id IN ({placeholders})", ids
    ).fetchall()
    return [_row_to_claim(r) for r in rows]


# Phase 14a: deterministic predicate→claim_type backfill. Kept as one SQL CASE
# (mirrors migration 007 and mesh_models.claim.PREDICATE_TO_CLAIM_TYPE) so the
# typing of stored claims is exact, free, and idempotent — re-running only
# rewrites rows whose claim_type drifted from what the predicate implies.
_CLAIM_TYPE_CASE = """
    CASE predicate
        WHEN 'achieves_score' THEN 'score'
        WHEN 'outperforms'    THEN 'comparison'
        WHEN 'developed_by'   THEN 'attribution'
        WHEN 'evaluated_on'   THEN 'evaluation'
        WHEN 'has_capability' THEN 'capability'
        WHEN 'based_on'       THEN 'lineage'
        WHEN 'reproduces'     THEN 'reproduction'
        WHEN 'critiques'      THEN 'critique'
        WHEN 'speculates'     THEN 'speculative'
        ELSE 'speculative'
    END
"""


def backfill_claim_types(conn: MeshConnection) -> int:
    """Recompute claim_type from predicate for any rows where it is NULL or has
    drifted. Returns the number of rows updated. Idempotent."""
    cur = conn.execute(
        f"UPDATE claims SET claim_type = {_CLAIM_TYPE_CASE} "
        f"WHERE claim_type IS DISTINCT FROM ({_CLAIM_TYPE_CASE})"
    )
    return cur.rowcount if cur.rowcount is not None else 0


def update_claim_status(
    conn: MeshConnection,
    id: str,
    status: ClaimStatus,
    superseded_by: str | None = None,
) -> Claim:
    conn.execute(
        "UPDATE claims SET status = %s, superseded_by_claim_id = %s WHERE id = %s",
        [status.value, superseded_by, id],
    )
    claim = get_claim_by_id(conn, id)
    if claim is None:
        raise ValueError(f"Claim {id} not found")
    return claim
