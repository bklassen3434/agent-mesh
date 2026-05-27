"""Phase 6b agent_tasks + agent_task_events DAL.

Orchestrator-side durability for A2A skill dispatch. The agent-side task
registry stays in-memory per the locked Phase 6b decisions; these tables
let the *caller* see what was in flight if the orchestrator crashed.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb
from pydantic import BaseModel, Field

# ── status + event-type literals ───────────────────────────────────────────

TASK_STATUS_PENDING = "pending"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"

EVENT_CREATED = "created"
EVENT_STARTED = "started"
EVENT_HEARTBEAT = "heartbeat"
EVENT_COMPLETED = "completed"
EVENT_FAILED = "failed"

ORPHANED_REASON = "orphaned_on_restart"


# ── Pydantic types ────────────────────────────────────────────────────────


class AgentTask(BaseModel):
    id: str
    skill_id: str
    agent_url: str
    status: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    dispatched_by_run_id: str | None = None


class AgentTaskEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    event_type: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    detail: dict[str, Any] | None = None


# ── writes ─────────────────────────────────────────────────────────────────


def create_task(
    conn: duckdb.DuckDBPyConnection,
    *,
    task_id: str,
    skill_id: str,
    agent_url: str,
    input_payload: dict[str, Any],
    dispatched_by_run_id: str | None = None,
) -> AgentTask:
    now = datetime.now(UTC)
    task = AgentTask(
        id=task_id,
        skill_id=skill_id,
        agent_url=agent_url,
        status=TASK_STATUS_PENDING,
        input=input_payload,
        created_at=now,
        updated_at=now,
        dispatched_by_run_id=dispatched_by_run_id,
    )
    conn.execute(
        """
        INSERT INTO agent_tasks
            (id, skill_id, agent_url, status, input, output, error,
             created_at, updated_at, dispatched_by_run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            task.id,
            task.skill_id,
            task.agent_url,
            task.status,
            json.dumps(task.input, default=str),
            None,
            None,
            task.created_at,
            task.updated_at,
            task.dispatched_by_run_id,
        ],
    )
    _record_event(conn, task_id, EVENT_CREATED)
    return task


def mark_running(conn: duckdb.DuckDBPyConnection, task_id: str) -> None:
    now = datetime.now(UTC)
    conn.execute(
        "UPDATE agent_tasks SET status = ?, updated_at = ? WHERE id = ?",
        [TASK_STATUS_RUNNING, now, task_id],
    )
    _record_event(conn, task_id, EVENT_STARTED)


def mark_heartbeat(conn: duckdb.DuckDBPyConnection, task_id: str) -> None:
    """Heartbeat doesn't change status — it just touches updated_at and
    appends an event row so the orphan sweep can tell live tasks from
    dead ones by recency."""
    now = datetime.now(UTC)
    conn.execute(
        "UPDATE agent_tasks SET updated_at = ? WHERE id = ?",
        [now, task_id],
    )
    _record_event(conn, task_id, EVENT_HEARTBEAT)


def mark_completed(
    conn: duckdb.DuckDBPyConnection,
    task_id: str,
    output_payload: dict[str, Any],
) -> None:
    now = datetime.now(UTC)
    conn.execute(
        """
        UPDATE agent_tasks
        SET status = ?, output = ?, updated_at = ?
        WHERE id = ?
        """,
        [
            TASK_STATUS_COMPLETED,
            json.dumps(output_payload, default=str),
            now,
            task_id,
        ],
    )
    _record_event(conn, task_id, EVENT_COMPLETED)


def mark_failed(
    conn: duckdb.DuckDBPyConnection,
    task_id: str,
    error: str,
    *,
    detail: dict[str, Any] | None = None,
) -> None:
    now = datetime.now(UTC)
    conn.execute(
        """
        UPDATE agent_tasks
        SET status = ?, error = ?, updated_at = ?
        WHERE id = ?
        """,
        [TASK_STATUS_FAILED, error, now, task_id],
    )
    _record_event(conn, task_id, EVENT_FAILED, detail=detail or {"error": error})


def _record_event(
    conn: duckdb.DuckDBPyConnection,
    task_id: str,
    event_type: str,
    *,
    detail: dict[str, Any] | None = None,
) -> None:
    event = AgentTaskEvent(task_id=task_id, event_type=event_type, detail=detail)
    conn.execute(
        """
        INSERT INTO agent_task_events (id, task_id, event_type, timestamp, detail)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            event.id,
            event.task_id,
            event.event_type,
            event.timestamp,
            json.dumps(event.detail, default=str) if event.detail is not None else None,
        ],
    )


# ── orphan sweep ───────────────────────────────────────────────────────────


def sweep_orphaned_tasks(
    conn: duckdb.DuckDBPyConnection,
    *,
    threshold_seconds: int,
    now: datetime | None = None,
) -> int:
    """Mark stale pending/running tasks as failed with orphaned_on_restart.

    Called by the coordinator + skeptic-sweep on startup. The threshold
    bounds how long a task can sit untouched (no heartbeats) before we
    treat it as dead — defaults to MESH_TASK_RESUME_THRESHOLD env var
    on the caller side, 600s out of the box. Returns the number of
    tasks updated.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=threshold_seconds)
    rows = conn.execute(
        """
        SELECT id FROM agent_tasks
        WHERE status IN (?, ?) AND updated_at < ?
        """,
        [TASK_STATUS_PENDING, TASK_STATUS_RUNNING, cutoff],
    ).fetchall()
    orphan_ids = [r[0] for r in rows]
    for task_id in orphan_ids:
        mark_failed(
            conn,
            task_id,
            ORPHANED_REASON,
            detail={"reason": ORPHANED_REASON, "threshold_seconds": threshold_seconds},
        )
    return len(orphan_ids)


# ── reads ──────────────────────────────────────────────────────────────────


def get_task(conn: duckdb.DuckDBPyConnection, task_id: str) -> AgentTask | None:
    row = conn.execute(
        """
        SELECT id, skill_id, agent_url, status, input, output, error,
               created_at, updated_at, dispatched_by_run_id
        FROM agent_tasks WHERE id = ?
        """,
        [task_id],
    ).fetchone()
    return _row_to_task(row) if row else None


def list_recent_failures(
    conn: duckdb.DuckDBPyConnection, limit: int = 10
) -> list[AgentTask]:
    rows = conn.execute(
        """
        SELECT id, skill_id, agent_url, status, input, output, error,
               created_at, updated_at, dispatched_by_run_id
        FROM agent_tasks
        WHERE status = ?
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        [TASK_STATUS_FAILED, limit],
    ).fetchall()
    return [_row_to_task(r) for r in rows]


def count_tasks_by_status(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM agent_tasks GROUP BY status"
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def _row_to_task(row: tuple[Any, ...]) -> AgentTask:
    (
        id_, skill_id, agent_url, status, input_raw, output_raw, error,
        created_at, updated_at, dispatched_by_run_id,
    ) = row[:10]

    def _json(raw: Any) -> dict[str, Any] | None:
        if raw is None:
            return None
        if isinstance(raw, dict):
            return raw
        return dict(json.loads(raw))

    return AgentTask(
        id=str(id_),
        skill_id=str(skill_id),
        agent_url=str(agent_url),
        status=str(status),
        input=_json(input_raw) or {},
        output=_json(output_raw),
        error=error,
        created_at=created_at,
        updated_at=updated_at,
        dispatched_by_run_id=dispatched_by_run_id,
    )
