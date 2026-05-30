from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from mesh_models.entity import Entity, EntityType
from psycopg.types.json import Jsonb

from mesh_db.connection import MeshConnection


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


def create_entity(conn: MeshConnection, model: Entity) -> Entity:
    conn.execute(
        """
        INSERT INTO entities
            (id, canonical_name, aliases, type, attributes, created_at, last_seen_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        [
            model.id,
            model.canonical_name,
            model.aliases,
            model.type.value,
            Jsonb(model.attributes),
            model.created_at,
            model.last_seen_at,
        ],
    )
    return model


def get_entity_by_id(conn: MeshConnection, id: str) -> Entity | None:
    row = conn.execute(
        "SELECT id, canonical_name, aliases, type, attributes, created_at, last_seen_at "
        "FROM entities WHERE id = %s",
        [id],
    ).fetchone()
    return _row_to_entity(row) if row else None


MAX_LIMIT = 200


def list_entities(
    conn: MeshConnection,
    type: EntityType | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Entity]:
    limit = min(max(limit, 0), MAX_LIMIT)
    offset = max(offset, 0)
    query = (
        "SELECT id, canonical_name, aliases, type, attributes, created_at, last_seen_at "
        "FROM entities"
    )
    conditions: list[str] = []
    params: list[Any] = []
    if type is not None:
        conditions.append("type = %s")
        params.append(type.value)
    if q:
        conditions.append("canonical_name ILIKE %s")
        params.append(f"%{q}%")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    return [_row_to_entity(r) for r in rows]


def count_entities(
    conn: MeshConnection,
    type: EntityType | None = None,
    q: str | None = None,
) -> int:
    query = "SELECT COUNT(*) FROM entities"
    conditions: list[str] = []
    params: list[Any] = []
    if type is not None:
        conditions.append("type = %s")
        params.append(type.value)
    if q:
        conditions.append("canonical_name ILIKE %s")
        params.append(f"%{q}%")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    row = conn.execute(query, params).fetchone()
    return int(row[0]) if row else 0


def get_entities_by_ids(
    conn: MeshConnection, ids: list[str]
) -> list[Entity]:
    if not ids:
        return []
    placeholders = ",".join(["%s"] * len(ids))
    rows = conn.execute(
        "SELECT id, canonical_name, aliases, type, attributes, created_at, last_seen_at "
        f"FROM entities WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    return [_row_to_entity(r) for r in rows]


def update_entity(
    conn: MeshConnection, id: str, **fields: Any
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
        set_clauses.append(f"{key} = %s")
        if key == "attributes":
            params.append(Jsonb(value))
        elif key == "type" and isinstance(value, EntityType):
            params.append(value.value)
        else:
            params.append(value)

    params.append(id)
    conn.execute(
        f"UPDATE entities SET {', '.join(set_clauses)} WHERE id = %s", params
    )
    entity = get_entity_by_id(conn, id)
    if entity is None:
        raise ValueError(f"Entity {id} not found after update")
    return entity
