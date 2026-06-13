"""Agent-observability domain models (Phase 23).

The durable per-skill-call capture (``AgentInvocation``) plus the read-side
aggregates the observability surface renders: the per-agent roster
(``AgentRosterEntry``) and the agent-interaction graph (``AgentGraph``).

These are I/O-free Pydantic models; the ``mesh_db.agent_invocations`` module
writes/reads them and the ``apps/api`` agents router serializes them. Unlike the
other domain models, ``AgentInvocation`` carries ``field_id`` directly (the
invocation *is* field-scoped state, and ``create_agent_invocation`` takes the
model whole), matching the table column.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from mesh_models.field import DEFAULT_FIELD_ID


class AgentInvocation(BaseModel):
    """One coordinator skill dispatch, captured for observability.

    Bounded by construction: ``input_summary`` / ``output_summary`` /
    ``memory_block`` are capped summaries written by the coordinator — the raw
    prompt/output lives in Langfuse, reachable via ``trace_id``.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    field_id: str = DEFAULT_FIELD_ID
    agent: str
    skill: str
    traceparent: str | None = None
    trace_id: str | None = None
    status: str = "ok"  # "ok" | "error"
    error_type: str | None = None
    error_message: str | None = None
    input_summary: dict[str, Any] | None = None
    output_summary: dict[str, Any] | None = None
    # Memory the agent injected, when it returns the optional debug envelope.
    memory_block: str | None = None
    applied_heuristic_ids: list[str] = Field(default_factory=list)
    system_prefix_hash: str | None = None
    model: str | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentRosterEntry(BaseModel):
    """Per-agent aggregate over a field's invocations — the roster row."""

    agent: str
    invocations: int = 0
    errors: int = 0
    error_rate: float = 0.0
    avg_latency_ms: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    last_active: datetime | None = None
    last_run_id: str | None = None


class AgentGraphNode(BaseModel):
    """A node in the agent-interaction graph.

    ``role`` distinguishes the dispatching ``coordinator`` hub from the
    dispatched ``agent`` nodes; ``invocation_count`` drives node size and
    ``error_rate`` drives node color in the cytoscape view.
    """

    id: str
    label: str
    role: str  # "coordinator" | "agent"
    invocation_count: int = 0
    error_rate: float = 0.0


class AgentGraphEdge(BaseModel):
    """A who-dispatches-whom edge; ``call_count`` drives stroke width."""

    source: str
    target: str
    call_count: int = 0
    error_count: int = 0


class AgentGraph(BaseModel):
    """Cytoscape-shaped agent-interaction graph for one field."""

    nodes: list[AgentGraphNode]
    edges: list[AgentGraphEdge]
