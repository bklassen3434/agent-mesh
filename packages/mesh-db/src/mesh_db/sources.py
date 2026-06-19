from __future__ import annotations

from datetime import datetime
from typing import Any

from mesh_models.field import DEFAULT_FIELD_ID
from mesh_models.source import Source, SourceType

from mesh_db.connection import MeshConnection


def _row_to_source(row: tuple[Any, ...]) -> Source:
    id_, type_, url, author, published_at, fetched_at, raw_content_hash, reliability_prior = (
        row[:8]
    )
    return Source(
        id=id_,
        type=SourceType(type_),
        url=url,
        author=author,
        published_at=(
            published_at if isinstance(published_at, datetime)
            else datetime.fromisoformat(str(published_at))
        ),
        fetched_at=(
            fetched_at if isinstance(fetched_at, datetime)
            else datetime.fromisoformat(str(fetched_at))
        ),
        raw_content_hash=raw_content_hash,
        reliability_prior=float(reliability_prior),
    )


def create_source(
    conn: MeshConnection, model: Source, *, field_id: str = DEFAULT_FIELD_ID
) -> Source:
    conn.execute(
        """
        INSERT INTO sources
            (id, field_id, type, url, author, published_at, fetched_at,
             raw_content_hash, reliability_prior)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            model.id,
            field_id,
            model.type.value,
            model.url,
            model.author,
            model.published_at,
            model.fetched_at,
            model.raw_content_hash,
            model.reliability_prior,
        ],
    )
    return model


def get_source_by_id(conn: MeshConnection, id: str) -> Source | None:
    row = conn.execute(
        "SELECT id, type, url, author, published_at, fetched_at, "
        "raw_content_hash, reliability_prior FROM sources WHERE id = %s",
        [id],
    ).fetchone()
    return _row_to_source(row) if row else None


MAX_LIMIT = 200

_SELECT = (
    "SELECT id, type, url, author, published_at, fetched_at, "
    "raw_content_hash, reliability_prior FROM sources"
)


def list_sources(
    conn: MeshConnection,
    type: SourceType | None = None,
    limit: int = 100,
    offset: int = 0,
    field_id: str | None = None,
) -> list[Source]:
    limit = min(max(limit, 0), MAX_LIMIT)
    offset = max(offset, 0)
    query = _SELECT
    conditions: list[str] = []
    params: list[Any] = []
    if field_id is not None:
        conditions.append("field_id = %s")
        params.append(field_id)
    if type is not None:
        conditions.append("type = %s")
        params.append(type.value)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY fetched_at DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    return [_row_to_source(r) for r in rows]


def count_sources(
    conn: MeshConnection,
    type: SourceType | None = None,
    field_id: str | None = None,
) -> int:
    query = "SELECT COUNT(*) FROM sources"
    conditions: list[str] = []
    params: list[Any] = []
    if field_id is not None:
        conditions.append("field_id = %s")
        params.append(field_id)
    if type is not None:
        conditions.append("type = %s")
        params.append(type.value)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    row = conn.execute(query, params).fetchone()
    return int(row[0]) if row else 0


def unextracted_sources(
    conn: MeshConnection,
    *,
    field_id: str = DEFAULT_FIELD_ID,
    limit: int = 50,
) -> list[Source]:
    """Sources in ``field_id`` that no claim references yet — the mesh has them
    but hasn't read them (the ``unextracted_source`` tension; agentic-migration
    Phase 0). Newest-first. ``agent_reasoning`` sources are skipped: they're
    synthesized rationale, not inputs to extract. A single anti-join, read-only,
    field-scoped."""
    limit = min(max(limit, 0), MAX_LIMIT)
    rows = conn.execute(
        _SELECT
        + """
        WHERE field_id = %s
          AND type <> 'agent_reasoning'
          AND NOT EXISTS (
              SELECT 1 FROM claims c WHERE c.source_id = sources.id
          )
        ORDER BY fetched_at DESC
        LIMIT %s
        """,
        [field_id, limit],
    ).fetchall()
    return [_row_to_source(r) for r in rows]


def get_sources_by_ids(
    conn: MeshConnection, ids: list[str]
) -> list[Source]:
    if not ids:
        return []
    placeholders = ",".join(["%s"] * len(ids))
    rows = conn.execute(
        f"{_SELECT} WHERE id IN ({placeholders})", ids
    ).fetchall()
    return [_row_to_source(r) for r in rows]


def update_source(
    conn: MeshConnection, id: str, **fields: Any
) -> Source:
    allowed = {"author", "reliability_prior"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        source = get_source_by_id(conn, id)
        if source is None:
            raise ValueError(f"Source {id} not found")
        return source

    set_clauses = [f"{k} = %s" for k in updates]
    params: list[Any] = list(updates.values())
    params.append(id)
    conn.execute(
        f"UPDATE sources SET {', '.join(set_clauses)} WHERE id = %s", params
    )
    source = get_source_by_id(conn, id)
    if source is None:
        raise ValueError(f"Source {id} not found after update")
    return source
