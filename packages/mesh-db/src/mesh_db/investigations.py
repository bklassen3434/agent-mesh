from __future__ import annotations

from datetime import datetime
from typing import Any

import duckdb
from mesh_models.investigation import Investigation, InvestigationStatus


def _row_to_investigation(row: tuple[Any, ...]) -> Investigation:
    (
        id_, question, related_entity_ids, status, priority,
        created_at, resolved_at, resolution_belief_id, assigned_scout_agents,
    ) = row[:9]
    return Investigation(
        id=id_,
        question=question,
        related_entity_ids=list(related_entity_ids) if related_entity_ids else [],
        status=InvestigationStatus(status),
        priority=float(priority),
        created_at=(
            created_at if isinstance(created_at, datetime)
            else datetime.fromisoformat(str(created_at))
        ),
        resolved_at=(
            resolved_at if resolved_at is None or isinstance(resolved_at, datetime)
            else datetime.fromisoformat(str(resolved_at))
        ),
        resolution_belief_id=resolution_belief_id,
        assigned_scout_agents=list(assigned_scout_agents) if assigned_scout_agents else [],
    )


_SELECT = (
    "SELECT id, question, related_entity_ids, status, priority, "
    "created_at, resolved_at, resolution_belief_id, assigned_scout_agents "
    "FROM investigations"
)


def create_investigation(
    conn: duckdb.DuckDBPyConnection, model: Investigation
) -> Investigation:
    conn.execute(
        """
        INSERT INTO investigations (id, question, related_entity_ids, status, priority,
            created_at, resolved_at, resolution_belief_id, assigned_scout_agents)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            model.id,
            model.question,
            model.related_entity_ids,
            model.status.value,
            model.priority,
            model.created_at,
            model.resolved_at,
            model.resolution_belief_id,
            model.assigned_scout_agents,
        ],
    )
    return model


def get_investigation_by_id(conn: duckdb.DuckDBPyConnection, id: str) -> Investigation | None:
    row = conn.execute(f"{_SELECT} WHERE id = ?", [id]).fetchone()
    return _row_to_investigation(row) if row else None


def list_investigations(
    conn: duckdb.DuckDBPyConnection,
    status: InvestigationStatus | None = None,
    limit: int = 100,
) -> list[Investigation]:
    params: list[Any] = []
    where = ""
    if status is not None:
        where = " WHERE status = ?"
        params.append(status.value)
    params.append(limit)
    rows = conn.execute(
        f"{_SELECT}{where} ORDER BY created_at DESC LIMIT ?", params
    ).fetchall()
    return [_row_to_investigation(r) for r in rows]


def update_investigation(
    conn: duckdb.DuckDBPyConnection, id: str, **fields: Any
) -> Investigation:
    allowed = {
        "status", "priority", "resolved_at", "resolution_belief_id",
        "assigned_scout_agents", "related_entity_ids",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        inv = get_investigation_by_id(conn, id)
        if inv is None:
            raise ValueError(f"Investigation {id} not found")
        return inv

    set_clauses = []
    params: list[Any] = []
    for key, value in updates.items():
        set_clauses.append(f"{key} = ?")
        if key == "status" and isinstance(value, InvestigationStatus):
            params.append(value.value)
        else:
            params.append(value)

    params.append(id)
    conn.execute(
        f"UPDATE investigations SET {', '.join(set_clauses)} WHERE id = ?", params
    )
    inv = get_investigation_by_id(conn, id)
    if inv is None:
        raise ValueError(f"Investigation {id} not found after update")
    return inv
