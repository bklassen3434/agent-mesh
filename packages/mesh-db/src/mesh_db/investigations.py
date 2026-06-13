from __future__ import annotations

from datetime import datetime
from typing import Any

from mesh_models.field import DEFAULT_FIELD_ID
from mesh_models.investigation import Investigation, InvestigationStatus

from mesh_db.connection import MeshConnection


def _row_to_investigation(row: tuple[Any, ...]) -> Investigation:
    (
        id_, question, related_entity_ids, status, priority,
        created_at, resolved_at, resolution_belief_id, assigned_scout_agents,
        target_entity_id, hypothesis, suggested_source_types,
        opened_by_belief_id, pipeline_runs_attempted, collected_claim_ids,
    ) = row[:15]
    return Investigation(
        id=id_,
        question=question,
        hypothesis=hypothesis,
        target_entity_id=target_entity_id,
        suggested_source_types=(
            list(suggested_source_types) if suggested_source_types else []
        ),
        opened_by_belief_id=opened_by_belief_id,
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
        pipeline_runs_attempted=int(pipeline_runs_attempted or 0),
        collected_claim_ids=list(collected_claim_ids) if collected_claim_ids else [],
    )


_SELECT = (
    "SELECT id, question, related_entity_ids, status, priority, "
    "created_at, resolved_at, resolution_belief_id, assigned_scout_agents, "
    "target_entity_id, hypothesis, suggested_source_types, "
    "opened_by_belief_id, pipeline_runs_attempted, collected_claim_ids "
    "FROM investigations"
)


def create_investigation(
    conn: MeshConnection, model: Investigation, *, field_id: str = DEFAULT_FIELD_ID
) -> Investigation:
    conn.execute(
        """
        INSERT INTO investigations (id, field_id, question, related_entity_ids, status,
            priority, created_at, resolved_at, resolution_belief_id,
            assigned_scout_agents, target_entity_id, hypothesis, suggested_source_types,
            opened_by_belief_id, pipeline_runs_attempted, collected_claim_ids)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            model.id,
            field_id,
            model.question,
            model.related_entity_ids,
            model.status.value,
            model.priority,
            model.created_at,
            model.resolved_at,
            model.resolution_belief_id,
            model.assigned_scout_agents,
            model.target_entity_id,
            model.hypothesis,
            model.suggested_source_types,
            model.opened_by_belief_id,
            model.pipeline_runs_attempted,
            model.collected_claim_ids,
        ],
    )
    return model


def get_investigation_by_id(conn: MeshConnection, id: str) -> Investigation | None:
    row = conn.execute(f"{_SELECT} WHERE id = %s", [id]).fetchone()
    return _row_to_investigation(row) if row else None


def list_investigations(
    conn: MeshConnection,
    status: InvestigationStatus | None = None,
    limit: int = 100,
    field_id: str | None = None,
) -> list[Investigation]:
    conditions: list[str] = []
    params: list[Any] = []
    if field_id is not None:
        conditions.append("field_id = %s")
        params.append(field_id)
    if status is not None:
        conditions.append("status = %s")
        params.append(status.value)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    rows = conn.execute(
        f"{_SELECT}{where} ORDER BY created_at DESC LIMIT %s", params
    ).fetchall()
    return [_row_to_investigation(r) for r in rows]


def update_investigation(
    conn: MeshConnection, id: str, **fields: Any
) -> Investigation:
    allowed = {
        "status", "priority", "resolved_at", "resolution_belief_id",
        "assigned_scout_agents", "related_entity_ids",
        "pipeline_runs_attempted", "collected_claim_ids",
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
        set_clauses.append(f"{key} = %s")
        if key == "status" and isinstance(value, InvestigationStatus):
            params.append(value.value)
        else:
            params.append(value)

    params.append(id)
    conn.execute(
        f"UPDATE investigations SET {', '.join(set_clauses)} WHERE id = %s", params
    )
    inv = get_investigation_by_id(conn, id)
    if inv is None:
        raise ValueError(f"Investigation {id} not found after update")
    return inv


def attach_claim_to_investigation(
    conn: MeshConnection,
    investigation_id: str,
    claim_id: str,
) -> Investigation:
    """Append a claim id to collected_claim_ids and bump the counter."""
    inv = get_investigation_by_id(conn, investigation_id)
    if inv is None:
        raise ValueError(f"Investigation {investigation_id} not found")
    if claim_id in inv.collected_claim_ids:
        return inv
    return update_investigation(
        conn,
        investigation_id,
        collected_claim_ids=[*inv.collected_claim_ids, claim_id],
    )
