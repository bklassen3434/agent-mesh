"""In-memory, asyncio-safe task registry used by mesh agent servers.

Phase 5a moves agent invocation from sync request/response to task-based
submit-then-poll. The TaskRegistry is the server-side store of in-flight
work: task_id -> status + result. State is ephemeral by design — Phase 5
explicitly defers durable task storage to Phase 6.

Each agent process owns one TaskRegistry; if the process restarts,
in-flight tasks vanish and the orchestrator's poll loop times out.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class TaskState(StrEnum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


@dataclass
class TaskRecord:
    task_id: str
    skill_id: str
    status: TaskState
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "skill_id": self.skill_id,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class TaskRegistry:
    """asyncio-safe in-memory task table."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tasks: dict[str, TaskRecord] = {}

    async def create(self, skill_id: str, metadata: dict[str, str] | None = None) -> TaskRecord:
        task_id = str(uuid.uuid4())
        record = TaskRecord(
            task_id=task_id,
            skill_id=skill_id,
            status=TaskState.pending,
            created_at=datetime.now(UTC),
            metadata=dict(metadata or {}),
        )
        async with self._lock:
            self._tasks[task_id] = record
        return record

    async def mark_running(self, task_id: str) -> None:
        async with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                return
            rec.status = TaskState.running
            rec.started_at = datetime.now(UTC)

    async def mark_completed(self, task_id: str, result: dict[str, Any]) -> None:
        async with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                return
            rec.status = TaskState.completed
            rec.result = result
            rec.finished_at = datetime.now(UTC)

    async def mark_failed(self, task_id: str, error: str) -> None:
        async with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                return
            rec.status = TaskState.failed
            rec.error = error
            rec.finished_at = datetime.now(UTC)

    async def get(self, task_id: str) -> TaskRecord | None:
        async with self._lock:
            return self._tasks.get(task_id)
