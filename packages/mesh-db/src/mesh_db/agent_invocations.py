"""The agent-invocation record — durable per-skill-call capture (Phase 23a).

One row per coordinator skill dispatch. Written exclusively by the coordinator
(the single writer); the agents stay write-free. Read by the ``/api/v1/agents*``
observability endpoints — the roster, an agent's recent invocations, one
invocation's full bounded detail, and the agent-interaction graph.

Append-only: ``create_agent_invocation`` inserts; there is intentionally no
``update_*`` or ``delete_*`` (mirrors claims / belief_revisions / llm_usage).
Every read is scoped to a ``field_id`` (field isolation, Phase 17a).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from mesh_models.agent_invocation import (
    AgentGraph,
    AgentGraphEdge,
    AgentGraphNode,
    AgentInvocation,
    AgentRosterEntry,
)
from mesh_models.field import DEFAULT_FIELD_ID
from psycopg.types.json import Jsonb

from mesh_db.connection import MeshConnection

_COORDINATOR_ID = "coordinator"

MAX_LIMIT = 500

_COLS = (
    "id, run_id, field_id, agent, skill, traceparent, trace_id, status, "
    "error_type, error_message, input_summary, output_summary, memory_block, "
    "applied_heuristic_ids, system_prefix_hash, model, latency_ms, "
    "input_tokens, output_tokens, cost_usd, created_at"
)


def create_agent_invocation(
    conn: MeshConnection, record: AgentInvocation
) -> AgentInvocation:
    """Insert one invocation row (coordinator is the single writer).

    ``field_id`` lives on the model itself (the invocation is field-scoped
    state), unlike the other ``create_*`` helpers that take it as a keyword."""
    conn.execute(
        """
        INSERT INTO agent_invocations
            (id, run_id, field_id, agent, skill, traceparent, trace_id, status,
             error_type, error_message, input_summary, output_summary,
             memory_block, applied_heuristic_ids, system_prefix_hash, model,
             latency_ms, input_tokens, output_tokens, cost_usd, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s)
        """,
        [
            record.id,
            record.run_id,
            record.field_id,
            record.agent,
            record.skill,
            record.traceparent,
            record.trace_id,
            record.status,
            record.error_type,
            record.error_message,
            None if record.input_summary is None else Jsonb(record.input_summary),
            None if record.output_summary is None else Jsonb(record.output_summary),
            record.memory_block,
            list(record.applied_heuristic_ids),
            record.system_prefix_hash,
            record.model,
            record.latency_ms,
            record.input_tokens,
            record.output_tokens,
            record.cost_usd,
            record.created_at,
        ],
    )
    return record


def list_agent_invocations(
    conn: MeshConnection,
    *,
    field_id: str = DEFAULT_FIELD_ID,
    agent: str | None = None,
    run_id: str | None = None,
    limit: int = 50,
) -> list[AgentInvocation]:
    """Recent invocations for a field, newest first. Optionally narrowed to one
    agent and/or one run."""
    limit = min(max(limit, 0), MAX_LIMIT)
    if limit == 0:
        return []
    where = ["field_id = %s"]
    params: list[Any] = [field_id]
    if agent is not None:
        where.append("agent = %s")
        params.append(agent)
    if run_id is not None:
        where.append("run_id = %s")
        params.append(run_id)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT {_COLS}
        FROM agent_invocations
        WHERE {' AND '.join(where)}
        ORDER BY created_at DESC
        LIMIT %s
        """,
        params,
    ).fetchall()
    return [_row_to_invocation(r) for r in rows]


def get_agent_invocation(
    conn: MeshConnection, invocation_id: str
) -> AgentInvocation | None:
    """One invocation by id (the full bounded detail), or ``None``."""
    row = conn.execute(
        f"SELECT {_COLS} FROM agent_invocations WHERE id = %s",
        [invocation_id],
    ).fetchone()
    return _row_to_invocation(row) if row is not None else None


def agent_roster(
    conn: MeshConnection, *, field_id: str = DEFAULT_FIELD_ID
) -> list[AgentRosterEntry]:
    """Per-agent aggregates for a field, busiest first: invocation count, error
    rate, avg latency, total tokens + cost, last-active + last run."""
    rows = conn.execute(
        """
        SELECT agent,
               COUNT(*)                                   AS invocations,
               COUNT(*) FILTER (WHERE status <> 'ok')     AS errors,
               AVG(latency_ms)                            AS avg_latency_ms,
               SUM(COALESCE(input_tokens, 0))             AS input_tokens,
               SUM(COALESCE(output_tokens, 0))            AS output_tokens,
               SUM(COALESCE(cost_usd, 0))                 AS cost_usd,
               MAX(created_at)                            AS last_active,
               (array_agg(run_id ORDER BY created_at DESC))[1] AS last_run_id
        FROM agent_invocations
        WHERE field_id = %s
        GROUP BY agent
        ORDER BY invocations DESC, agent
        """,
        [field_id],
    ).fetchall()
    out: list[AgentRosterEntry] = []
    for r in rows:
        invocations = int(r[1])
        errors = int(r[2])
        out.append(
            AgentRosterEntry(
                agent=str(r[0]),
                invocations=invocations,
                errors=errors,
                error_rate=(errors / invocations) if invocations else 0.0,
                avg_latency_ms=float(r[3] or 0.0),
                total_input_tokens=int(r[4] or 0),
                total_output_tokens=int(r[5] or 0),
                total_cost_usd=float(r[6] or 0.0),
                last_active=r[7] if isinstance(r[7], datetime) else None,
                last_run_id=None if r[8] is None else str(r[8]),
            )
        )
    return out


def agent_graph(
    conn: MeshConnection, *, field_id: str = DEFAULT_FIELD_ID
) -> AgentGraph:
    """The agent-interaction graph for a field, cytoscape-shaped.

    Topology: a single ``coordinator`` hub dispatches every agent (that is the
    real call topology — agents never dispatch each other), so the graph is a
    star. Agent nodes are sized by invocation volume and colored by error rate;
    each edge's ``call_count`` is the coordinator→agent dispatch volume."""
    roster = agent_roster(conn, field_id=field_id)
    nodes: list[AgentGraphNode] = [
        AgentGraphNode(
            id=_COORDINATOR_ID,
            label="coordinator",
            role="coordinator",
            invocation_count=sum(e.invocations for e in roster),
        )
    ]
    edges: list[AgentGraphEdge] = []
    for entry in roster:
        nodes.append(
            AgentGraphNode(
                id=entry.agent,
                label=entry.agent,
                role="agent",
                invocation_count=entry.invocations,
                error_rate=entry.error_rate,
            )
        )
        edges.append(
            AgentGraphEdge(
                source=_COORDINATOR_ID,
                target=entry.agent,
                call_count=entry.invocations,
                error_count=entry.errors,
            )
        )
    return AgentGraph(nodes=nodes, edges=edges)


def _row_to_invocation(row: tuple[Any, ...]) -> AgentInvocation:
    created_at = row[20]
    if not isinstance(created_at, datetime):
        created_at = datetime.fromisoformat(str(created_at))
    return AgentInvocation(
        id=str(row[0]),
        run_id=str(row[1]),
        field_id=str(row[2]),
        agent=str(row[3]),
        skill=str(row[4]),
        traceparent=None if row[5] is None else str(row[5]),
        trace_id=None if row[6] is None else str(row[6]),
        status=str(row[7]),
        error_type=None if row[8] is None else str(row[8]),
        error_message=None if row[9] is None else str(row[9]),
        input_summary=row[10],
        output_summary=row[11],
        memory_block=None if row[12] is None else str(row[12]),
        applied_heuristic_ids=list(row[13]) if row[13] is not None else [],
        system_prefix_hash=None if row[14] is None else str(row[14]),
        model=None if row[15] is None else str(row[15]),
        latency_ms=None if row[16] is None else int(row[16]),
        input_tokens=None if row[17] is None else int(row[17]),
        output_tokens=None if row[18] is None else int(row[18]),
        cost_usd=None if row[19] is None else float(row[19]),
        created_at=created_at,
    )
