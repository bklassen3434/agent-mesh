"""Phase 6b orchestrator-side task persistence injection point.

``MeshA2AClient`` calls into a ``TaskRecorder`` from inside
``call_skill_blocking`` to record dispatch lifecycle events. The concrete
recorder is provided by the orchestrator (which owns the DuckDB
connection); mesh-a2a only knows the protocol so the wire-protocol
package stays free of DB dependencies.

Callers who don't pass a recorder get the ``NullTaskRecorder`` no-op,
preserving the existing call signature contract (5a-locked):
``call_skill_blocking`` only gains side effects, never changes shape.
"""
from __future__ import annotations

from typing import Any, Protocol


class TaskRecorder(Protocol):
    """Lifecycle hooks invoked by ``call_skill_blocking``.

    Implementations are expected to be synchronous (DuckDB writes are
    sub-millisecond) and never raise — recorder failures must not break
    skill dispatch.
    """

    def record_pending(
        self,
        *,
        task_id: str,
        skill_id: str,
        agent_url: str,
        payload: dict[str, Any],
    ) -> None: ...

    def record_running(self, task_id: str) -> None: ...

    def record_heartbeat(self, task_id: str) -> None: ...

    def record_completed(self, task_id: str, output: dict[str, Any]) -> None: ...

    def record_failed(self, task_id: str, error: str) -> None: ...


class NullTaskRecorder:
    """No-op recorder used when the caller doesn't supply one."""

    def record_pending(
        self,
        *,
        task_id: str,
        skill_id: str,
        agent_url: str,
        payload: dict[str, Any],
    ) -> None:
        pass

    def record_running(self, task_id: str) -> None:
        pass

    def record_heartbeat(self, task_id: str) -> None:
        pass

    def record_completed(self, task_id: str, output: dict[str, Any]) -> None:
        pass

    def record_failed(self, task_id: str, error: str) -> None:
        pass
