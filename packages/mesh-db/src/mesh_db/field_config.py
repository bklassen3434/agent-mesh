"""Per-field numeric config overrides — the install target for config-kind actuators.

Keys are namespaced by component ("confidence.attack_weight", …). Append-only:
installing a new value deactivates the prior active one and inserts the new active.
"""
from __future__ import annotations

import uuid

from mesh_db.connection import MeshConnection


def get_active_config(conn: MeshConnection, field_id: str) -> dict[str, float]:
    """All active config overrides for a field, ``key → value``."""
    rows = conn.execute(
        "SELECT key, value FROM field_config WHERE field_id = %s AND is_active",
        [field_id],
    ).fetchall()
    return {r[0]: float(r[1]) for r in rows}


def set_field_config(
    conn: MeshConnection, field_id: str, values: dict[str, float], *, rationale: str = ""
) -> None:
    """Install ``values`` as the active overrides for ``field_id`` — deactivate the
    prior active row per key, then insert the new active one. Append-only content."""
    for key, value in values.items():
        conn.execute(
            "UPDATE field_config SET is_active = false "
            "WHERE field_id = %s AND key = %s AND is_active",
            [field_id, key],
        )
        conn.execute(
            "INSERT INTO field_config (id, field_id, key, value, is_active, rationale) "
            "VALUES (%s, %s, %s, %s, true, %s)",
            [str(uuid.uuid4()), field_id, key, float(value), rationale],
        )
