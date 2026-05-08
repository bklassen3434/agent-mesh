from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import duckdb
from mesh_models.entity import Entity, EntityType


def _row_to_entity(row: tuple[Any, ...]) -> Entity:
    id_, canonical_name, aliases, type_, attributes, created_at, last_seen_at = row[:7]
    return Entity(
        id=id_,
        canonical_name=canonical_name,
        aliases=list(aliases) if aliases else [],
        type=EntityType(type_),
        attributes=json.loads(attributes) if isinstance(attributes, str) else (attributes or {}),
        created_at=(
            created_at if isinstance(created_at, datetime)
            else datetime.fromisoformat(str(created_at))
        ),
        last_seen_at=(
            last_seen_at if isinstance(last_seen_at, datetime)
            else datetime.fromisoformat(str(last_seen_at))
        ),
    )


def create_entity(conn: duckdb.DuckDBPyConnection, model: Entity) -> Entity:
    conn.execute(
        """
        INSERT INTO entities
            (id, canonical_name, aliases, type, attributes, created_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            model.id,
            model.canonical_name,
            model.aliases,
            model.type.value,
            json.dumps(model.attributes),
            model.created_at,
            model.last_seen_at,
        ],
    )
    return model


def get_entity_by_id(conn: duckdb.DuckDBPyConnection, id: str) -> Entity | None:
    row = conn.execute(
        "SELECT id, canonical_name, aliases, type, attributes, created_at, last_seen_at "
        "FROM entities WHERE id = ?",
        [id],
    ).fetchone()
    return _row_to_entity(row) if row else None


def list_entities(
    conn: duckdb.DuckDBPyConnection,
    type: EntityType | None = None,
    limit: int = 100,
) -> list[Entity]:
    query = (
        "SELECT id, canonical_name, aliases, type, attributes, created_at, last_seen_at "
        "FROM entities"
    )
    params: list[Any] = []
    if type is not None:
        query += " WHERE type = ?"
        params.append(type.value)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [_row_to_entity(r) for r in rows]


def update_entity(
    conn: duckdb.DuckDBPyConnection, id: str, **fields: Any
) -> Entity:
    allowed = {"canonical_name", "aliases", "type", "attributes", "last_seen_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        entity = get_entity_by_id(conn, id)
        if entity is None:
            raise ValueError(f"Entity {id} not found")
        return entity

    set_clauses = []
    params: list[Any] = []
    for key, value in updates.items():
        set_clauses.append(f"{key} = ?")
        if key == "attributes":
            params.append(json.dumps(value))
        elif key == "type" and isinstance(value, EntityType):
            params.append(value.value)
        else:
            params.append(value)

    params.append(id)
    conn.execute(
        f"UPDATE entities SET {', '.join(set_clauses)} WHERE id = ?", params
    )
    entity = get_entity_by_id(conn, id)
    if entity is None:
        raise ValueError(f"Entity {id} not found after update")
    return entity
