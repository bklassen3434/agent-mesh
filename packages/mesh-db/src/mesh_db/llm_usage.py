"""Per-call LLM token + cost ledger (Phase 11a).

One row per LLM skill call, keyed by ``run_id``. Written exclusively by the
coordinator / skeptic-sweep graphs (the single writer); agents thread
their token usage back through the A2A skill response. Read by
``mesh.cli cost report`` to attribute spend per skill for a run.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from mesh_db.connection import MeshConnection


class LLMUsageRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    agent_name: str | None = None
    skill_id: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    estimated_cost_usd: float = 0.0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SkillUsageTotals(BaseModel):
    skill_id: str
    agent_name: str | None = None
    model: str | None = None
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    estimated_cost_usd: float = 0.0


def create_llm_usage(
    conn: MeshConnection, record: LLMUsageRecord
) -> LLMUsageRecord:
    conn.execute(
        """
        INSERT INTO llm_usage
            (id, run_id, agent_name, skill_id, model, input_tokens, output_tokens,
             cache_read_tokens, cache_creation_tokens, estimated_cost_usd, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            record.id,
            record.run_id,
            record.agent_name,
            record.skill_id,
            record.model,
            record.input_tokens,
            record.output_tokens,
            record.cache_read_tokens,
            record.cache_creation_tokens,
            record.estimated_cost_usd,
            record.created_at,
        ],
    )
    return record


def aggregate_usage_by_skill(
    conn: MeshConnection, run_id: str
) -> list[SkillUsageTotals]:
    """Per-skill token + cost totals for a run, ordered by cost descending."""
    rows = conn.execute(
        """
        SELECT skill_id,
               any_value(agent_name) AS agent_name,
               any_value(model)      AS model,
               COUNT(*)              AS calls,
               SUM(input_tokens)     AS input_tokens,
               SUM(output_tokens)    AS output_tokens,
               SUM(cache_read_tokens) AS cache_read_tokens,
               SUM(cache_creation_tokens) AS cache_creation_tokens,
               SUM(estimated_cost_usd) AS estimated_cost_usd
        FROM llm_usage
        WHERE run_id = %s
        GROUP BY skill_id
        ORDER BY estimated_cost_usd DESC
        """,
        [run_id],
    ).fetchall()
    return [
        SkillUsageTotals(
            skill_id=str(r[0]),
            agent_name=None if r[1] is None else str(r[1]),
            model=None if r[2] is None else str(r[2]),
            calls=int(r[3]),
            input_tokens=int(r[4] or 0),
            output_tokens=int(r[5] or 0),
            cache_read_tokens=int(r[6] or 0),
            cache_creation_tokens=int(r[7] or 0),
            estimated_cost_usd=float(r[8] or 0.0),
        )
        for r in rows
    ]


class ModelUsageTotals(BaseModel):
    """Per-model token + cost totals aggregated across runs (Phase 20)."""

    model: str | None = None
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    estimated_cost_usd: float = 0.0


def aggregate_usage_by_model(
    conn: MeshConnection,
    *,
    field_id: str | None = None,
    since: datetime | None = None,
) -> list[ModelUsageTotals]:
    """Per-model token + cost totals, optionally scoped to a field and a time
    window. Powers ``mesh.cli routing-stats`` — the before/after evidence that
    tiered routing is paying off, read straight from the existing ledger.

    ``field_id`` filters by the run's field (joined via ``pipeline_runs``);
    ``since`` filters by the usage row's ``created_at``.
    """
    where: list[str] = []
    params: list[Any] = []
    join = ""
    if field_id is not None:
        join = "JOIN pipeline_runs r ON r.id = u.run_id"
        where.append("r.field_id = %s")
        params.append(field_id)
    if since is not None:
        where.append("u.created_at >= %s")
        params.append(since)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"""
        SELECT u.model,
               COUNT(*)                    AS calls,
               SUM(u.input_tokens)         AS input_tokens,
               SUM(u.output_tokens)        AS output_tokens,
               SUM(u.cache_read_tokens)    AS cache_read_tokens,
               SUM(u.cache_creation_tokens) AS cache_creation_tokens,
               SUM(u.estimated_cost_usd)   AS estimated_cost_usd
        FROM llm_usage u
        {join}
        {where_sql}
        GROUP BY u.model
        ORDER BY estimated_cost_usd DESC
        """,
        params,
    ).fetchall()
    return [
        ModelUsageTotals(
            model=None if r[0] is None else str(r[0]),
            calls=int(r[1]),
            input_tokens=int(r[2] or 0),
            output_tokens=int(r[3] or 0),
            cache_read_tokens=int(r[4] or 0),
            cache_creation_tokens=int(r[5] or 0),
            estimated_cost_usd=float(r[6] or 0.0),
        )
        for r in rows
    ]


def list_llm_usage(
    conn: MeshConnection, run_id: str
) -> list[LLMUsageRecord]:
    rows = conn.execute(
        """
        SELECT id, run_id, agent_name, skill_id, model, input_tokens, output_tokens,
               cache_read_tokens, cache_creation_tokens, estimated_cost_usd, created_at
        FROM llm_usage
        WHERE run_id = %s
        ORDER BY created_at
        """,
        [run_id],
    ).fetchall()
    return [_row_to_record(r) for r in rows]


def _row_to_record(row: tuple[Any, ...]) -> LLMUsageRecord:
    created_at = row[10]
    if not isinstance(created_at, datetime):
        created_at = datetime.fromisoformat(str(created_at))
    return LLMUsageRecord(
        id=str(row[0]),
        run_id=str(row[1]),
        agent_name=None if row[2] is None else str(row[2]),
        skill_id=str(row[3]),
        model=None if row[4] is None else str(row[4]),
        input_tokens=int(row[5] or 0),
        output_tokens=int(row[6] or 0),
        cache_read_tokens=int(row[7] or 0),
        cache_creation_tokens=int(row[8] or 0),
        estimated_cost_usd=float(row[9] or 0.0),
        created_at=created_at,
    )
