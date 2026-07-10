"""Field briefs — append-only LLM narratives for the Field Overview page."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from mesh_models.field_brief import FieldBrief
from psycopg.types.json import Jsonb

from mesh_db.connection import MeshConnection


def create_field_brief(conn: MeshConnection, brief: FieldBrief) -> FieldBrief:
    conn.execute(
        """
        INSERT INTO field_briefs (id, field_id, narrative, model, inputs_summary, generated_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        [
            brief.id,
            brief.field_id,
            brief.narrative,
            brief.model,
            Jsonb(brief.inputs_summary),
            brief.generated_at,
        ],
    )
    return brief


def get_latest_field_brief(conn: MeshConnection, field_id: str) -> FieldBrief | None:
    row = conn.execute(
        """
        SELECT id, field_id, narrative, model, inputs_summary, generated_at
        FROM field_briefs
        WHERE field_id = %s
        ORDER BY generated_at DESC
        LIMIT 1
        """,
        [field_id],
    ).fetchone()
    if row is None:
        return None
    return _row_to_brief(row)


def _row_to_brief(row: tuple[Any, ...]) -> FieldBrief:
    id_, field_id, narrative, model, inputs_summary, generated_at = row[:6]
    return FieldBrief(
        id=id_,
        field_id=field_id,
        narrative=narrative,
        model=model or "",
        inputs_summary=dict(inputs_summary) if inputs_summary else {},
        generated_at=(
            generated_at
            if isinstance(generated_at, datetime)
            else datetime.fromisoformat(str(generated_at))
        ),
    )
