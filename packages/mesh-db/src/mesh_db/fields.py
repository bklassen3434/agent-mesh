"""Field access layer (Phase 17a).

A Field is the first-class scope that partitions all field-state. This module is
the typed interface over ``knowledge.fields``: reader-safe reads (``get_field`` /
``get_field_by_slug`` / ``list_fields``) and writer-only writes (``create_field`` /
``set_active``). ``seed_default_field`` upserts the canonical ``ai-robotics``
profile and is called by ``init_pg`` so Python — not the SQL migration literal —
is the source of truth for the seeded profile.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import psycopg
from mesh_models.field import AI_ROBOTICS_PROFILE, DEFAULT_FIELD_ID, Field, FieldProfile
from psycopg.types.json import Jsonb

from mesh_db.connection import MeshConnection

_SELECT = (
    "SELECT id, name, slug, profile, created_at, is_active FROM fields"
)


def _row_to_field(row: tuple[Any, ...]) -> Field:
    id_, name, slug, profile, created_at, is_active = row
    profile_obj = json.loads(profile) if isinstance(profile, str) else profile
    return Field(
        id=str(id_),
        name=str(name),
        slug=str(slug),
        profile=FieldProfile.model_validate(profile_obj),
        created_at=(
            created_at if isinstance(created_at, datetime)
            else datetime.fromisoformat(str(created_at))
        ),
        is_active=bool(is_active),
    )


def get_field(conn: MeshConnection, field_id: str) -> Field | None:
    row = conn.execute(f"{_SELECT} WHERE id = %s", [field_id]).fetchone()
    return _row_to_field(row) if row else None


def get_field_by_slug(conn: MeshConnection, slug: str) -> Field | None:
    row = conn.execute(f"{_SELECT} WHERE slug = %s", [slug]).fetchone()
    return _row_to_field(row) if row else None


def list_fields(conn: MeshConnection, *, active_only: bool = False) -> list[Field]:
    query = _SELECT
    if active_only:
        query += " WHERE is_active = TRUE"
    query += " ORDER BY created_at ASC"
    rows = conn.execute(query).fetchall()
    return [_row_to_field(r) for r in rows]


def create_field(conn: MeshConnection, model: Field) -> Field:
    conn.execute(
        """
        INSERT INTO fields (id, name, slug, profile, created_at, is_active)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        [
            model.id,
            model.name,
            model.slug,
            Jsonb(model.profile.model_dump()),
            model.created_at,
            model.is_active,
        ],
    )
    return model


def set_active(conn: MeshConnection, field_id: str, active: bool) -> Field:
    conn.execute(
        "UPDATE fields SET is_active = %s WHERE id = %s", [active, field_id]
    )
    field = get_field(conn, field_id)
    if field is None:
        raise ValueError(f"Field {field_id} not found")
    return field


def seed_default_field(conn: psycopg.Connection[Any]) -> None:
    """Upsert the canonical ``ai-robotics`` field profile. Idempotent.

    Called by ``init_pg`` after migrations so the full FieldProfile (whose
    few-shot text the SQL runner cannot carry safely) is materialized from the
    Python constant. Runs on the owner connection used for migrations.
    """
    profile_json = json.dumps(AI_ROBOTICS_PROFILE.model_dump())
    conn.execute(
        """
        INSERT INTO knowledge.fields (id, name, slug, profile)
        VALUES (%s, %s, %s, %s::jsonb)
        ON CONFLICT (id) DO UPDATE
            SET name = EXCLUDED.name,
                slug = EXCLUDED.slug,
                profile = EXCLUDED.profile
        """,
        [
            DEFAULT_FIELD_ID,
            AI_ROBOTICS_PROFILE.name,
            AI_ROBOTICS_PROFILE.slug,
            profile_json,
        ],
    )
    conn.commit()
