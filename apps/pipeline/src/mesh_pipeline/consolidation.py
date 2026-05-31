"""Memory consolidation as a LangGraph graph (Phase 16c).

Offline distillation of an agent's recent episodic + outcome history (Phase 15)
into durable procedural heuristics (Phase 16b). Cloned from
``skeptic_sweep.py``: same checkpointing (thread_id == run_id), traceparent,
Batch-API path with a synchronous fallback, finalize-idempotency guard, and
Langfuse cost attribution.

Graph shape::

    START → load_history ─[history?]→ submit_batch | distill_one (fan-out) | finalize
      submit_batch → poll_batch → collect_results → finalize
      distill_one (fan-out) → finalize → END

No hot-path LLM: this runs offline (scheduler-fired, batch by default). No new
service — the existing scheduler shells out to ``mesh-consolidate`` like it does
``mesh-skeptic-sweep``. Writes go through the coordinator-writer role; agents
never write the procedural store.
"""
from __future__ import annotations

import asyncio
import operator
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, TypedDict, cast

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from mesh_a2a.checkpoint import open_checkpointer, thread_config
from mesh_a2a.tracing import new_traceparent
from mesh_agents.consolidator import (
    ConsolidationResult,
    build_consolidation_prompt,
    candidate_to_proposal,
    distill_pure,
)
from mesh_db.connection import get_connection
from mesh_db.episodic import EpisodicEntry, recall_history
from mesh_db.heuristics import list_heuristics
from mesh_db.llm_usage import LLMUsageRecord, create_llm_usage
from mesh_db.pg_migrations import init_pg
from mesh_db.pipeline_runs import PipelineRun, create_pipeline_run, pipeline_run_exists
from mesh_llm import (
    AnthropicClient,
    BatchRequestItem,
    LLMProviderNotReadyError,
    make_llm_client,
)
from mesh_llm.pricing import estimate_cost
from mesh_llm.usage import LLMUsage
from mesh_tracing.tracing import trace_generation
from pydantic import BaseModel

from mesh_pipeline._heuristics import persist_heuristic

log = structlog.get_logger()

# (agent, skill) pairs to consolidate — the Phase-16a wired LLM skills. Each
# agent id matches the identity stamped on its artifacts (recall_history key).
_DEFAULT_TARGETS: list[tuple[str, str]] = [
    ("claim_extractor", "extract_claims"),
    ("skeptic", "challenge_belief"),
]

# Model role for env-driven routing (MESH_LLM_MODEL_CONSOLIDATOR → default).
_CONSOLIDATOR_AGENT = "consolidator"


class ConsolidationRunResult(BaseModel):
    run_id: str
    targets_considered: int
    targets_with_history: int
    heuristics_written: int


# ── env knobs (mirror the skeptic-sweep batch knobs) ─────────────────────────


def _history_limit() -> int:
    return int(os.environ.get("MESH_CONSOLIDATION_HISTORY_LIMIT", "50"))


def _ttl_days() -> int:
    return int(os.environ.get("MESH_CONSOLIDATION_TTL_DAYS", "30"))


def _batch_enabled() -> bool:
    return os.environ.get("MESH_CONSOLIDATION_BATCH", "true").lower() in (
        "1", "true", "yes",
    )


def _batch_poll_seconds() -> float:
    return float(os.environ.get("MESH_CONSOLIDATION_BATCH_POLL_SECONDS", "15"))


def _batch_timeout_seconds() -> float:
    return float(os.environ.get("MESH_CONSOLIDATION_BATCH_TIMEOUT_SECONDS", "85000"))


def _targets() -> list[tuple[str, str]]:
    raw = os.environ.get("MESH_CONSOLIDATION_TARGETS", "")
    if not raw:
        return _DEFAULT_TARGETS
    out: list[tuple[str, str]] = []
    for pair in raw.split(","):
        agent, _, skill = pair.strip().partition(":")
        if agent and skill:
            out.append((agent, skill))
    return out or _DEFAULT_TARGETS


# ── graph state ──────────────────────────────────────────────────────────────


class ConsolidationState(TypedDict):
    run_id: str
    triggered_by: str
    traceparent: str
    started_at: str  # ISO-8601
    # per target: {agent, skill, entries: [EpisodicEntry json], run_ids, claim_ids}
    targets: list[dict[str, Any]]
    batch_id: str | None
    heuristics_written: Annotated[int, operator.add]
    usages: Annotated[list[dict[str, Any]], operator.add]
    targets_considered: int
    targets_with_history: int
    errors: Annotated[list[dict[str, Any]], operator.add]
    finalized: bool


class _DistillWork(TypedDict):
    """Per-target payload for the distill_one fan-out worker."""

    agent: str
    skill: str
    entries: list[dict[str, Any]]
    run_ids: list[str]
    claim_ids: list[str]


# ── helpers ──────────────────────────────────────────────────────────────────


def _provenance_from_entries(
    entries: list[EpisodicEntry],
) -> tuple[list[str], list[str]]:
    """Collect the runs + claims an agent's history was drawn from — the
    provenance every distilled heuristic links back to."""
    run_ids: set[str] = set()
    claim_ids: set[str] = set()
    for e in entries:
        if e.run_id:
            run_ids.add(e.run_id)
        for cid in e.refs.get("claim_ids", []) or []:
            claim_ids.add(str(cid))
        for cid in e.refs.get("trigger_claim_ids", []) or []:
            claim_ids.add(str(cid))
    return sorted(run_ids), sorted(claim_ids)


# Cap on how many existing heuristics to scan for the dedup check.
MAX_DEDUP = 200


def _already_present(conn: Any, agent: str, skill: str, text: str) -> bool:
    """Skip a candidate if an active, unexpired heuristic with identical text
    already exists for this scope — avoids flooding the store with re-distilled
    duplicates across scheduled runs."""
    existing = list_heuristics(
        conn, agent=agent, skill=skill, active=True, include_expired=False, limit=MAX_DEDUP,
    )
    return any(h.heuristic.strip() == text.strip() for h in existing)


def _persist_candidates(
    conn: Any,
    agent: str,
    candidates: list[Any],
    run_ids: list[str],
    claim_ids: list[str],
) -> int:
    written = 0
    ttl = _ttl_days()
    for candidate in candidates:
        if not run_ids and not claim_ids:
            log.warning("consolidation_skip_no_provenance", agent=agent)
            continue
        if _already_present(conn, agent, candidate.skill, candidate.heuristic):
            continue
        proposal = candidate_to_proposal(
            agent, candidate, run_ids=run_ids, claim_ids=claim_ids, ttl_days=ttl
        )
        persist_heuristic(conn, proposal, revised_by_agent=_CONSOLIDATOR_AGENT)
        written += 1
    return written


def _usage_row(agent: str, usage: LLMUsage, model: str, *, batch: bool) -> dict[str, Any]:
    return {"agent": agent, "usage": usage.model_dump(), "model": model, "batch": batch}


# ── graph construction ───────────────────────────────────────────────────────


def build_consolidation_graph(
    conn: Any, batch_llm: AnthropicClient | None, sync_llm: Any | None
) -> StateGraph[ConsolidationState, Any, Any, Any]:
    """Build the consolidation graph. ``batch_llm`` (anthropic + batch on) routes
    distillation through the Batch API; otherwise the synchronous ``sync_llm``
    fan-out is used."""

    async def load_history(state: ConsolidationState) -> dict[str, Any]:
        targets = _targets()
        limit = _history_limit()
        built: list[dict[str, Any]] = []
        for agent, skill in targets:
            entries = recall_history(conn, agent, limit=limit)
            if not entries:
                continue
            run_ids, claim_ids = _provenance_from_entries(entries)
            if not run_ids and not claim_ids:
                # No provenance to ground a heuristic in — skip (provenance is
                # mandatory).
                continue
            built.append(
                {
                    "agent": agent,
                    "skill": skill,
                    "entries": [e.model_dump(mode="json") for e in entries],
                    "run_ids": run_ids,
                    "claim_ids": claim_ids,
                }
            )
        log.info(
            "consolidation_history_loaded",
            targets=len(targets),
            with_history=len(built),
        )
        return {
            "targets": built,
            "targets_considered": len(targets),
            "targets_with_history": len(built),
        }

    def route_after_load(state: ConsolidationState) -> list[Send] | str:
        if not state["targets"]:
            return "finalize"
        if batch_llm is not None:
            return "submit_batch"
        return [
            Send(
                "distill_one",
                {
                    "agent": t["agent"],
                    "skill": t["skill"],
                    "entries": t["entries"],
                    "run_ids": t["run_ids"],
                    "claim_ids": t["claim_ids"],
                },
            )
            for t in state["targets"]
        ]

    async def distill_one(state: _DistillWork) -> dict[str, Any]:
        assert sync_llm is not None
        agent = state["agent"]
        entries = [EpisodicEntry.model_validate(e) for e in state["entries"]]
        result, usage, model = await asyncio.to_thread(
            distill_pure, sync_llm, agent, state["skill"], entries
        )
        written = _persist_candidates(
            conn, agent, result.heuristics, state["run_ids"], state["claim_ids"]
        )
        log.info(
            "consolidation_distilled",
            agent=agent,
            candidates=len(result.heuristics),
            written=written,
            batch=False,
        )
        return {
            "heuristics_written": written,
            "usages": [_usage_row(agent, usage, model, batch=False)],
        }

    async def submit_batch(state: ConsolidationState) -> dict[str, Any]:
        assert batch_llm is not None
        items: list[BatchRequestItem] = []
        for t in state["targets"]:
            entries = [EpisodicEntry.model_validate(e) for e in t["entries"]]
            system, user = build_consolidation_prompt(t["agent"], t["skill"], entries)
            items.append(BatchRequestItem(custom_id=t["agent"], system=system, user=user))
        if not items:
            return {"batch_id": None}
        batch_id = await asyncio.to_thread(
            batch_llm.submit_batch, items, ConsolidationResult
        )
        log.info("consolidation_batch_submitted", batch_id=batch_id, requests=len(items))
        return {"batch_id": batch_id}

    def route_after_submit(state: ConsolidationState) -> str:
        return "poll_batch" if state.get("batch_id") else "finalize"

    async def poll_batch(state: ConsolidationState) -> dict[str, Any]:
        assert batch_llm is not None
        batch_id = state["batch_id"]
        if not batch_id:
            return {}
        deadline = time.monotonic() + _batch_timeout_seconds()
        while True:
            status = await asyncio.to_thread(batch_llm.batch_status, batch_id)
            if status == "ended":
                log.info("consolidation_batch_ended", batch_id=batch_id)
                break
            if time.monotonic() > deadline:
                log.warning("consolidation_batch_timeout", batch_id=batch_id, status=status)
                break
            await asyncio.sleep(_batch_poll_seconds())
        return {}

    async def collect_results(state: ConsolidationState) -> dict[str, Any]:
        assert batch_llm is not None
        batch_id = state["batch_id"]
        if not batch_id:
            return {}
        results = await asyncio.to_thread(
            batch_llm.collect_batch, batch_id, ConsolidationResult
        )
        written = 0
        usages: list[dict[str, Any]] = []
        by_agent = {t["agent"]: t for t in state["targets"]}
        for agent, t in by_agent.items():
            res = results.get(agent)
            if res is None or res.parsed is None:
                log.warning(
                    "consolidation_batch_item_failed",
                    agent=agent,
                    error=res.error if res else "missing result",
                )
                continue
            result = cast(ConsolidationResult, res.parsed)
            n = _persist_candidates(
                conn, agent, result.heuristics, t["run_ids"], t["claim_ids"]
            )
            written += n
            # Trace the batched generation to Langfuse at the discounted rate.
            entries = [EpisodicEntry.model_validate(e) for e in t["entries"]]
            system, user = build_consolidation_prompt(agent, t["skill"], entries)
            cost = estimate_cost(res.model, res.usage, batch=True)
            trace_generation(
                name="consolidate_heuristics",
                model=res.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                output=result.model_dump_json(),
                latency_ms=0,
                usage=res.usage.model_dump(),
                cost_usd=cost.total_cost,
                agent_name=_CONSOLIDATOR_AGENT,
            )
            usages.append(_usage_row(agent, res.usage, res.model, batch=True))
            log.info(
                "consolidation_distilled",
                agent=agent,
                candidates=len(result.heuristics),
                written=n,
                batch=True,
            )
        return {"heuristics_written": written, "usages": usages}

    async def finalize(state: ConsolidationState) -> dict[str, Any]:
        if pipeline_run_exists(conn, state["run_id"]):
            log.info("finalize_already_done", run_id=state["run_id"])
            return {"finalized": True}
        # Per-skill cost attribution: one llm_usage row per target (agent).
        for u in state["usages"]:
            usage = LLMUsage(**(u.get("usage") or {}))
            if usage.total_tokens == 0:
                continue
            model = str(u.get("model") or "")
            create_llm_usage(
                conn,
                LLMUsageRecord(
                    run_id=state["run_id"],
                    agent_name=str(u.get("agent") or _CONSOLIDATOR_AGENT),
                    skill_id="consolidate_heuristics",
                    model=model or None,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_creation_tokens=usage.cache_creation_tokens,
                    estimated_cost_usd=estimate_cost(
                        model, usage, batch=bool(u.get("batch", False))
                    ).total_cost,
                ),
            )
        run = PipelineRun(
            id=state["run_id"],
            run_type="consolidation",
            started_at=datetime.fromisoformat(state["started_at"]),
            finished_at=datetime.now(UTC),
            triggered_by=state["triggered_by"],
            beliefs_created=0,
        )
        create_pipeline_run(conn, run)
        log.info(
            "consolidation_complete",
            run_id=run.id,
            targets_with_history=state["targets_with_history"],
            heuristics_written=state["heuristics_written"],
        )
        return {"finalized": True}

    g: StateGraph[ConsolidationState, Any, Any, Any] = StateGraph(ConsolidationState)
    g.add_node("load_history", load_history)
    g.add_node("distill_one", distill_one, input_schema=_DistillWork)
    g.add_node("submit_batch", submit_batch)
    g.add_node("poll_batch", poll_batch)
    g.add_node("collect_results", collect_results)
    g.add_node("finalize", finalize)

    g.add_edge(START, "load_history")
    g.add_conditional_edges(
        "load_history", route_after_load, ["distill_one", "submit_batch", "finalize"]
    )
    g.add_edge("distill_one", "finalize")
    g.add_conditional_edges("submit_batch", route_after_submit, ["poll_batch", "finalize"])
    g.add_edge("poll_batch", "collect_results")
    g.add_edge("collect_results", "finalize")
    g.add_edge("finalize", END)
    return g


def _result_from_state(state: ConsolidationState) -> ConsolidationRunResult:
    return ConsolidationRunResult(
        run_id=state["run_id"],
        targets_considered=state["targets_considered"],
        targets_with_history=state["targets_with_history"],
        heuristics_written=state["heuristics_written"],
    )


async def run_consolidation(db_path: str | None = None) -> ConsolidationRunResult:
    """Top-level entry point — the `mesh-consolidate` console script calls this."""
    log.info("consolidation_starting")

    conn = get_connection(db_path)
    init_pg()

    run_id = os.environ.get("MESH_RUN_ID") or str(uuid.uuid4())
    initial_state: ConsolidationState = {
        "run_id": run_id,
        "triggered_by": os.environ.get("MESH_TRIGGERED_BY", "manual"),
        "traceparent": new_traceparent(),
        "started_at": datetime.now(UTC).isoformat(),
        "targets": [],
        "batch_id": None,
        "heuristics_written": 0,
        "usages": [],
        "targets_considered": 0,
        "targets_with_history": 0,
        "errors": [],
        "finalized": False,
    }

    # Batch path is on by default but only for the anthropic provider; otherwise
    # fall back to the synchronous LLM. The model is env-routed for the
    # consolidator role (MESH_LLM_MODEL_CONSOLIDATOR → default).
    batch_llm: AnthropicClient | None = None
    sync_llm: Any | None = None
    try:
        candidate = make_llm_client(agent_name=_CONSOLIDATOR_AGENT)
    except LLMProviderNotReadyError as exc:
        log.info("consolidation_provider_unavailable", error=str(exc))
        candidate = None
    if candidate is not None:
        if _batch_enabled() and isinstance(candidate, AnthropicClient):
            batch_llm = candidate
            log.info("consolidation_batch_mode", model=batch_llm.model)
        else:
            sync_llm = candidate

    graph = build_consolidation_graph(conn, batch_llm, sync_llm)
    async with open_checkpointer() as saver:
        app = graph.compile(checkpointer=saver)
        final = await app.ainvoke(initial_state, config=thread_config(run_id))
    conn.close()
    return _result_from_state(cast(ConsolidationState, final))


def main() -> None:
    """Console-script entry point: `uv run mesh-consolidate`."""
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ]
    )
    db_path = os.environ.get("MESH_DB_PATH")
    result = asyncio.run(run_consolidation(db_path=db_path))
    print(f"\nConsolidation {result.run_id}")
    print(f"  Targets considered:   {result.targets_considered}")
    print(f"  Targets with history: {result.targets_with_history}")
    print(f"  Heuristics written:   {result.heuristics_written}")
