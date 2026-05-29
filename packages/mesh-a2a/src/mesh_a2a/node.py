"""LangGraph node wrapper around A2A skill dispatch (Phase 8).

The coordinator + skeptic-sweep graphs call agents through this thin
wrapper. It preserves the error-handling philosophy the imperative
coordinators relied on: a single skill failure is *recorded*, never
raised — the graph continues and folds the error into ``state["errors"]``.

``call_skill_node`` is deliberately low-level: it returns
``(result, error)`` rather than a state patch, because each graph node
stashes its result under a different state key. Nodes call this and
build their own ``{key: [...], "errors": [...]}`` patch, which LangGraph
merges via the ``operator.add`` reducers on those channels.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from mesh_a2a.client import MeshA2AClient, SkillCallError, SkillNotFoundError


class TaskError(BaseModel):
    """A recorded skill-dispatch failure carried in graph state.

    Stored in state as ``model_dump()`` dicts so checkpoints stay
    JSON-native and the status page can read them without importing
    pipeline types.
    """

    skill_id: str
    error_type: str
    error_message: str
    # Optional breadcrumb identifying *which* unit of work failed
    # (e.g. {"arxiv_id": "..."} or {"belief_id": "..."}), so a fan-out
    # failure can be traced back to its item.
    context: dict[str, Any] = Field(default_factory=dict)


async def call_skill_node(
    client: MeshA2AClient,
    skill_id: str,
    payload: dict[str, Any],
    *,
    traceparent: str | None = None,
    context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, TaskError | None]:
    """Dispatch one A2A skill from inside a LangGraph node.

    Never raises. On success returns ``(result, None)``; on any failure
    returns ``(None, TaskError)`` so the calling node can append the
    error to ``state["errors"]`` and let the graph proceed. The
    traceparent is forwarded to ``call_skill_blocking`` so distributed
    tracing survives the LangGraph migration.
    """
    try:
        result = await client.call_skill_blocking(
            skill_id, payload, traceparent=traceparent
        )
        return result, None
    except (SkillNotFoundError, SkillCallError) as exc:
        return None, TaskError(
            skill_id=skill_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
            context=context or {},
        )
    except Exception as exc:  # defensive: a node failure must not abort the graph
        return None, TaskError(
            skill_id=skill_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
            context=context or {},
        )
