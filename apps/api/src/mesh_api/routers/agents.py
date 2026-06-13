"""GET /api/v1/agents* — agent-observability read API (Phase 23b).

Read-only, field-scoped surface over the agent_invocations record (and the
existing heuristic/episodic memory readers): the per-agent roster, an agent's
recent invocations, one invocation's full bounded detail (with a Langfuse
deep-link for the raw prompt), the agent's *current* learned memory, and the
agent-interaction graph. Mirrors the other routers — ``mesh_reader`` connection,
``?field=`` scoping, Pydantic response models.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Query
from mesh_db.agent_invocations import (
    agent_graph,
    agent_roster,
    get_agent_invocation,
    list_agent_invocations,
)
from mesh_db.episodic import recall_history
from mesh_db.heuristics import get_heuristic_by_id, list_heuristics
from mesh_models.agent_invocation import AgentGraph, AgentInvocation, AgentRosterEntry
from mesh_models.heuristic import AgentHeuristic
from pydantic import BaseModel

from mesh_api.deps import ConnDep

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

_FIELD = Query("ai-robotics", description="Field slug to scope results to")


class ResolvedHeuristic(BaseModel):
    """An applied heuristic id resolved to its text (for the invocation view)."""

    id: str
    heuristic: str
    confidence: float


class AgentInvocationDetail(BaseModel):
    """One invocation's full bounded detail + resolved memory + Langfuse link."""

    invocation: AgentInvocation
    applied_heuristics: list[ResolvedHeuristic]
    langfuse_url: str | None = None


class AgentMemory(BaseModel):
    """An agent's *current* learned state: active heuristics + recent history."""

    agent: str
    heuristics: list[AgentHeuristic]
    episodic: list[dict[str, object]]


def _langfuse_trace_url(trace_id: str | None) -> str | None:
    """Deep-link to a trace in Langfuse, when configured. Best-effort: returns
    None unless a Langfuse base URL is set."""
    if not trace_id:
        return None
    base = os.environ.get("NEXT_PUBLIC_LANGFUSE_URL") or os.environ.get("LANGFUSE_HOST")
    if not base:
        return None
    return f"{base.rstrip('/')}/trace/{trace_id}"


@router.get(
    "",
    response_model=list[AgentRosterEntry],
    summary="Agent roster",
    description=(
        "Per-agent aggregates for a field, busiest first: invocation count, "
        "error rate, avg latency, total tokens + cost, last-active + last run."
    ),
)
def get_roster(conn: ConnDep, field: str = _FIELD) -> list[AgentRosterEntry]:
    return agent_roster(conn, field_id=field)


@router.get(
    "/graph",
    response_model=AgentGraph,
    summary="Agent-interaction graph",
    description=(
        "Cytoscape-shaped who-dispatches-whom graph: a single coordinator hub "
        "dispatches every agent. Node size = invocation volume, color = error "
        "rate; edge width = dispatch volume."
    ),
)
def get_agent_graph(conn: ConnDep, field: str = _FIELD) -> AgentGraph:
    return agent_graph(conn, field_id=field)


@router.get(
    "/invocations/{invocation_id}",
    response_model=AgentInvocationDetail,
    summary="One invocation, full bounded detail",
    description=(
        "The full bounded input/output, the memory block + applied heuristic "
        "ids (resolved to their current text), model/tokens/cost, and a Langfuse "
        "deep-link to the raw prompt (when Langfuse is configured)."
    ),
)
def get_invocation(invocation_id: str, conn: ConnDep) -> AgentInvocationDetail:
    inv = get_agent_invocation(conn, invocation_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invocation not found")
    resolved: list[ResolvedHeuristic] = []
    for hid in inv.applied_heuristic_ids:
        h = get_heuristic_by_id(conn, hid)
        if h is not None:
            resolved.append(
                ResolvedHeuristic(id=h.id, heuristic=h.heuristic, confidence=h.confidence)
            )
    return AgentInvocationDetail(
        invocation=inv,
        applied_heuristics=resolved,
        langfuse_url=_langfuse_trace_url(inv.trace_id),
    )


@router.get(
    "/{agent}/invocations",
    response_model=list[AgentInvocation],
    summary="An agent's recent invocations",
    description="Recent invocations for one agent in a field, newest first.",
)
def get_agent_invocations(
    agent: str,
    conn: ConnDep,
    field: str = _FIELD,
    limit: int = Query(50, ge=1, le=200),
) -> list[AgentInvocation]:
    return list_agent_invocations(conn, field_id=field, agent=agent, limit=limit)


@router.get(
    "/{agent}/memory",
    response_model=AgentMemory,
    summary="An agent's current learned memory",
    description=(
        "What this agent knows now: its active, unexpired heuristics and its "
        "recent episodic history (reconstructed from its artifacts)."
    ),
)
def get_agent_memory(
    agent: str,
    conn: ConnDep,
    field: str = _FIELD,
    limit: int = Query(50, ge=1, le=200),
) -> AgentMemory:
    heuristics = list_heuristics(
        conn, agent=agent, active=True, include_expired=False,
        limit=limit, field_id=field,
    )
    episodic = recall_history(conn, agent, limit=limit, field_id=field)
    return AgentMemory(
        agent=agent,
        heuristics=heuristics,
        episodic=[e.model_dump(mode="json") for e in episodic],
    )
