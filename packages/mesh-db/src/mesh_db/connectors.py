"""Connector catalog + per-field enablement access layer (Phase 17c).

Reader-safe reads over the global ``catalog.connectors`` catalog and the
per-field ``catalog.field_connectors`` enablement; writer-only writes
(``enable_connector`` validates config against the connector's ``config_schema``
before persisting, so bad config is rejected at write time). ``seed_connectors``
materializes the built-in catalog + the ai-robotics enablement from the Python
registry and is called by ``init_pg``.
"""
from __future__ import annotations

import json
from typing import Any

import psycopg
from mesh_models.connector import (
    AI_ROBOTICS_FIELD_CONNECTORS,
    BUILTIN_CONNECTORS,
    Connector,
    ConnectorKind,
    FieldConnector,
    validate_connector_config,
)
from psycopg.types.json import Jsonb

from mesh_db.connection import MeshConnection


def _json(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else (value or {})


def _row_to_connector(row: tuple[Any, ...]) -> Connector:
    id_, slug, name, description, kind, config_schema = row[:6]
    return Connector(
        id=str(id_),
        slug=str(slug),
        name=str(name),
        description=str(description),
        kind=ConnectorKind(kind),
        config_schema=_json(config_schema),
    )


_CONN_SELECT = (
    "SELECT id, slug, name, description, kind, config_schema FROM connectors"
)


def list_connectors(conn: MeshConnection) -> list[Connector]:
    rows = conn.execute(f"{_CONN_SELECT} ORDER BY slug").fetchall()
    return [_row_to_connector(r) for r in rows]


def get_connector(conn: MeshConnection, connector_id: str) -> Connector | None:
    row = conn.execute(f"{_CONN_SELECT} WHERE id = %s", [connector_id]).fetchone()
    return _row_to_connector(row) if row else None


def _row_to_field_connector(row: tuple[Any, ...]) -> FieldConnector:
    field_id, connector_id, config, enabled = row[:4]
    return FieldConnector(
        field_id=str(field_id),
        connector_id=str(connector_id),
        config=_json(config),
        enabled=bool(enabled),
    )


def list_field_connectors(
    conn: MeshConnection, field_id: str, *, enabled_only: bool = False
) -> list[FieldConnector]:
    """The connectors configured for a field. ``enabled_only`` restricts to the
    ones a run would dispatch."""
    query = (
        "SELECT field_id, connector_id, config, enabled FROM field_connectors "
        "WHERE field_id = %s"
    )
    params: list[Any] = [field_id]
    if enabled_only:
        query += " AND enabled = TRUE"
    query += " ORDER BY connector_id"
    rows = conn.execute(query, params).fetchall()
    return [_row_to_field_connector(r) for r in rows]


def enable_connector(
    conn: MeshConnection,
    field_id: str,
    connector_id: str,
    *,
    config: dict[str, Any] | None = None,
    enabled: bool = True,
) -> FieldConnector:
    """Enable (or reconfigure) a connector for a field. Validates ``config``
    against the connector's ``config_schema`` and rejects bad config at write
    time. Coordinator/writer-owned."""
    connector = get_connector(conn, connector_id)
    if connector is None:
        raise ValueError(f"unknown connector '{connector_id}'")
    cfg = config or {}
    validate_connector_config(cfg, connector.config_schema)
    conn.execute(
        """
        INSERT INTO field_connectors (field_id, connector_id, config, enabled)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (field_id, connector_id) DO UPDATE SET
            config = excluded.config,
            enabled = excluded.enabled,
            updated_at = now()
        """,
        [field_id, connector_id, Jsonb(cfg), enabled],
    )
    return FieldConnector(
        field_id=field_id, connector_id=connector_id, config=cfg, enabled=enabled
    )


def seed_connectors(conn: psycopg.Connection[Any]) -> None:
    """Upsert the built-in catalog + the ai-robotics field enablement. Idempotent.

    Called by ``init_pg`` (owner connection) so the connector config_schema and
    the seed config live in Python (mesh_models.connector), not the SQL literal."""
    for c in BUILTIN_CONNECTORS:
        conn.execute(
            """
            INSERT INTO catalog.connectors (id, slug, name, description, kind, config_schema)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (id) DO UPDATE SET
                slug = EXCLUDED.slug,
                name = EXCLUDED.name,
                description = EXCLUDED.description,
                kind = EXCLUDED.kind,
                config_schema = EXCLUDED.config_schema
            """,
            [c.id, c.slug, c.name, c.description, c.kind.value, json.dumps(c.config_schema)],
        )
    for fc in AI_ROBOTICS_FIELD_CONNECTORS:
        # Don't clobber an operator's later edits — only seed missing rows.
        conn.execute(
            """
            INSERT INTO catalog.field_connectors (field_id, connector_id, config, enabled)
            VALUES (%s, %s, %s::jsonb, %s)
            ON CONFLICT (field_id, connector_id) DO NOTHING
            """,
            [fc.field_id, fc.connector_id, json.dumps(fc.config), fc.enabled],
        )
    conn.commit()
