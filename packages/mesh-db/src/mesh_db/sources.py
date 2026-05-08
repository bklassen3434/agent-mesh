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


def list_sources(
    conn: duckdb.DuckDBPyConnection,
    type: SourceType | None = None,
    limit: int = 100,
) -> list[Source]:
    query = (
        "SELECT id, type, url, author, published_at, fetched_at, "
        "raw_content_hash, reliability_prior FROM sources"
    )
    params: list[Any] = []
    if type is not None:
        query += " WHERE type = ?"
        params.append(type.value)
    query += " ORDER BY fetched_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
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
