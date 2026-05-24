from __future__ import annotations

from datetime import datetime
from typing import Any

import duckdb
from mesh_models.source import Source, SourceType


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


def create_source(conn: duckdb.DuckDBPyConnection, model: Source) -> Source:
    conn.execute(
        """
        INSERT INTO sources
            (id, type, url, author, published_at, fetched_at, raw_content_hash, reliability_prior)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            model.id,
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


def get_source_by_id(conn: duckdb.DuckDBPyConnection, id: str) -> Source | None:
    row = conn.execute(
        "SELECT id, type, url, author, published_at, fetched_at, "
        "raw_content_hash, reliability_prior FROM sources WHERE id = ?",
        [id],
    ).fetchone()
    return _row_to_source(row) if row else None


MAX_LIMIT = 200

_SELECT = (
    "SELECT id, type, url, author, published_at, fetched_at, "
    "raw_content_hash, reliability_prior FROM sources"
)


def list_sources(
    conn: duckdb.DuckDBPyConnection,
    type: SourceType | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Source]:
    limit = min(max(limit, 0), MAX_LIMIT)
    offset = max(offset, 0)
    query = _SELECT
    params: list[Any] = []
    if type is not None:
        query += " WHERE type = ?"
        params.append(type.value)
    query += " ORDER BY fetched_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    return [_row_to_source(r) for r in rows]


def count_sources(
    conn: duckdb.DuckDBPyConnection,
    type: SourceType | None = None,
) -> int:
    query = "SELECT COUNT(*) FROM sources"
    params: list[Any] = []
    if type is not None:
        query += " WHERE type = ?"
        params.append(type.value)
    row = conn.execute(query, params).fetchone()
    return int(row[0]) if row else 0


def get_sources_by_ids(
    conn: duckdb.DuckDBPyConnection, ids: list[str]
) -> list[Source]:
    if not ids:
        return []
    placeholders = ",".join(["?"] * len(ids))
    rows = conn.execute(
        f"{_SELECT} WHERE id IN ({placeholders})", ids
    ).fetchall()
    return [_row_to_source(r) for r in rows]


def update_source(
    conn: duckdb.DuckDBPyConnection, id: str, **fields: Any
) -> Source:
    allowed = {"author", "reliability_prior"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        source = get_source_by_id(conn, id)
        if source is None:
            raise ValueError(f"Source {id} not found")
        return source

    set_clauses = [f"{k} = ?" for k in updates]
    params: list[Any] = list(updates.values())
    params.append(id)
    conn.execute(
        f"UPDATE sources SET {', '.join(set_clauses)} WHERE id = ?", params
    )
    source = get_source_by_id(conn, id)
    if source is None:
        raise ValueError(f"Source {id} not found after update")
    return source
