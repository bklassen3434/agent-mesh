from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import duckdb
from mesh_models.claim import Claim, ClaimStatus


def _row_to_claim(row: tuple[Any, ...]) -> Claim:
    (
        id_, predicate, subject_entity_id, object_, source_id,
        extracted_at, extracted_by_agent, raw_excerpt, status,
        confidence, superseded_by_claim_id,
    ) = row[:11]
    return Claim(
        id=id_,
        predicate=predicate,
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
    )


_SELECT = (
    "SELECT id, predicate, subject_entity_id, object, source_id, "
    "extracted_at, extracted_by_agent, raw_excerpt, status, confidence, superseded_by_claim_id "
    "FROM claims"
)


def create_claim(conn: duckdb.DuckDBPyConnection, model: Claim) -> Claim:
    conn.execute(
        """
        INSERT INTO claims
            (id, predicate, subject_entity_id, object, source_id,
            extracted_at, extracted_by_agent, raw_excerpt, status, confidence,
            superseded_by_claim_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            model.id,
            model.predicate,
            model.subject_entity_id,
            json.dumps(model.object),
            model.source_id,
            model.extracted_at,
            model.extracted_by_agent,
            model.raw_excerpt,
            model.status.value,
            model.confidence,
            model.superseded_by_claim_id,
        ],
    )
    return model


def get_claim_by_id(conn: duckdb.DuckDBPyConnection, id: str) -> Claim | None:
    row = conn.execute(f"{_SELECT} WHERE id = ?", [id]).fetchone()
    return _row_to_claim(row) if row else None


MAX_LIMIT = 200


def _claim_filters(
    entity_id: str | None,
    source_id: str | None,
    status: ClaimStatus | None,
    predicate: str | None,
) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if entity_id is not None:
        conditions.append("subject_entity_id = ?")
        params.append(entity_id)
    if source_id is not None:
        conditions.append("source_id = ?")
        params.append(source_id)
    if status is not None:
        conditions.append("status = ?")
        params.append(status.value)
    if predicate is not None:
        conditions.append("predicate = ?")
        params.append(predicate)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return where, params


def list_claims(
    conn: duckdb.DuckDBPyConnection,
    entity_id: str | None = None,
    source_id: str | None = None,
    status: ClaimStatus | None = None,
    predicate: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Claim]:
    limit = min(max(limit, 0), MAX_LIMIT)
    offset = max(offset, 0)
    where, params = _claim_filters(entity_id, source_id, status, predicate)
    params.extend([limit, offset])
    rows = conn.execute(
        f"{_SELECT}{where} ORDER BY extracted_at DESC LIMIT ? OFFSET ?", params
    ).fetchall()
    return [_row_to_claim(r) for r in rows]


def count_claims(
    conn: duckdb.DuckDBPyConnection,
    entity_id: str | None = None,
    source_id: str | None = None,
    status: ClaimStatus | None = None,
    predicate: str | None = None,
) -> int:
    where, params = _claim_filters(entity_id, source_id, status, predicate)
    row = conn.execute(f"SELECT COUNT(*) FROM claims{where}", params).fetchone()
    return int(row[0]) if row else 0


def get_claims_by_ids(
    conn: duckdb.DuckDBPyConnection, ids: list[str]
) -> list[Claim]:
    if not ids:
        return []
    placeholders = ",".join(["?"] * len(ids))
    rows = conn.execute(
        f"{_SELECT} WHERE id IN ({placeholders})", ids
    ).fetchall()
    return [_row_to_claim(r) for r in rows]


def update_claim_status(
    conn: duckdb.DuckDBPyConnection,
    id: str,
    status: ClaimStatus,
    superseded_by: str | None = None,
) -> Claim:
    conn.execute(
        "UPDATE claims SET status = ?, superseded_by_claim_id = ? WHERE id = ?",
        [status.value, superseded_by, id],
    )
    claim = get_claim_by_id(conn, id)
    if claim is None:
        raise ValueError(f"Claim {id} not found")
    return claim
