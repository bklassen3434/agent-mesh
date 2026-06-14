"""Belief consolidation as a LangGraph graph (Phase 19d/19e).

Offline, scheduled sweep that keeps each field's held belief corpus coherent:
it semantically de-duplicates beliefs (block → match → merge, conservative bands
with batch-API adjudication in the ambiguous middle), then ages stale ones
(confidence decay + archival). Cloned from ``consolidation.py``: same
checkpointing (thread_id == run_id), traceparent, Batch-API path with a
synchronous fallback, finalize-idempotency guard, and Langfuse cost attribution.

Graph shape::

    START → load_candidates ─[middle pairs?]→ submit_batch | finalize-ish
      load_candidates → (apply high-band merges immediately)
      submit_batch → poll_batch → collect_results (adjudicate + apply merges)
      collect_results / load_candidates → decay → finalize → END

Append-only and coordinator-writer-owned: every merge / decay / archive records a
``BeliefRevision`` attributed to ``belief_consolidator``; no row is deleted, no
claim is touched. Field-scoped — the sweep iterates active fields and never
compares or merges across them. No hot-path LLM: adjudication runs offline
(batch by default). No new service — the existing scheduler shells out to
``mesh-consolidate-beliefs`` like it does ``mesh-consolidate-memory``.
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
from mesh_a2a.checkpoint import open_checkpointer, thread_config
from mesh_a2a.tracing import new_traceparent
from mesh_agents.belief_consolidation import (
    BeliefForMatch,
    BeliefMatchDecision,
    BeliefMergeConfig,
    build_belief_adjudication_prompt,
    make_confidence_fn,
)
from mesh_agents.belief_reconcile import (
    block_and_band,
    cluster_and_merge,
    decay_and_archive,
    ensure_belief_embeddings,
    load_candidate_beliefs,
)
from mesh_agents.confidence import ConfidenceWeights
from mesh_db.connection import get_connection
from mesh_db.fields import list_fields
from mesh_db.llm_usage import LLMUsageRecord, create_llm_usage
from mesh_db.pg_migrations import init_pg
from mesh_db.pipeline_runs import PipelineRun, create_pipeline_run, pipeline_run_exists
from mesh_llm import AnthropicClient, LLMProviderNotReadyError, make_llm_client
from mesh_llm.pricing import estimate_cost
from mesh_llm.usage import LLMUsage
from mesh_tracing.tracing import trace_generation
from pydantic import BaseModel

log = structlog.get_logger()

_AGENT = "belief_consolidator"


class BeliefConsolidationRunResult(BaseModel):
    run_id: str
    fields_processed: int
    beliefs_merged: int
    beliefs_decayed: int
    beliefs_archived: int


# ── env knobs (mirror the consolidation batch knobs) ─────────────────────────


def _batch_enabled() -> bool:
    return os.environ.get("MESH_BELIEF_CONSOLIDATION_BATCH", "true").lower() in (
        "1", "true", "yes",
    )


def _batch_poll_seconds() -> float:
    return float(os.environ.get("MESH_BELIEF_CONSOLIDATION_BATCH_POLL_SECONDS", "15"))


def _batch_timeout_seconds() -> float:
    return float(os.environ.get("MESH_BELIEF_CONSOLIDATION_BATCH_TIMEOUT_SECONDS", "85000"))


# ── graph state ──────────────────────────────────────────────────────────────


class _MiddlePair(TypedDict):
    field_id: str
    a_id: str
    b_id: str
    a_topic: str
    a_statement: str
    b_topic: str
    b_statement: str


class BeliefConsolidationState(TypedDict):
    run_id: str
    triggered_by: str
    traceparent: str
    started_at: str  # ISO-8601
    # confirmed-same pairs to merge (high band + adjudication-confirmed middle).
    # ALL merges are deferred to a single apply_merges node so high + middle
    # cluster together — a belief in both a high pair and a middle pair is then
    # folded onto one canonical once, never merged twice across phases.
    confirmed_pairs: Annotated[list[dict[str, str]], operator.add]
    # middle-band pairs awaiting batch/sync adjudication
    middle_pairs: list[_MiddlePair]
    batch_id: str | None
    fields_processed: int
    beliefs_merged: Annotated[int, operator.add]
    beliefs_decayed: Annotated[int, operator.add]
    beliefs_archived: Annotated[int, operator.add]
    usages: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[dict[str, Any]], operator.add]
    finalized: bool


# ── helpers ──────────────────────────────────────────────────────────────────


def _usage_row(usage: LLMUsage, model: str, *, batch: bool) -> dict[str, Any]:
    return {"usage": usage.model_dump(), "model": model, "batch": batch}


def _apply_confirmed(
    conn: Any, pairs: list[dict[str, str]], confidence_fn: Any
) -> int:
    """Cluster + merge confirmed-same pairs (across all fields; pairs are already
    same-field by construction). Returns beliefs absorbed."""
    confirmed = {frozenset({p["a_id"], p["b_id"]}) for p in pairs}
    records = cluster_and_merge(
        conn, confirmed, confidence_fn=confidence_fn, dry_run=False
    )
    merged = sum(len(r.absorbed) for r in records)
    for r in records:
        log.info(
            "belief_merge_applied",
            canonical=r.canonical_id,
            absorbed=[bid for bid, _ in r.absorbed],
        )
    return merged


# ── graph construction ───────────────────────────────────────────────────────


def build_belief_consolidation_graph(
    conn: Any, batch_llm: AnthropicClient | None, sync_llm: Any | None
) -> StateGraph[BeliefConsolidationState, Any, Any, Any]:
    """Build the belief-consolidation graph. ``batch_llm`` (anthropic + batch on)
    routes middle-band adjudication through the Batch API; otherwise the
    synchronous ``sync_llm`` is used inline (or, with neither, the middle band
    defaults to not-same)."""
    cfg = BeliefMergeConfig.from_env()
    weights = ConfidenceWeights.from_env()
    confidence_fn = make_confidence_fn(weights)
    embedder = _make_embedder()

    async def load_candidates(state: BeliefConsolidationState) -> dict[str, Any]:
        # Per active field: backfill embeddings, block + band the candidate set,
        # stage high-band pairs (confirmed) and middle pairs (to adjudicate). No
        # merge happens here — all merges are deferred to apply_merges so high +
        # middle cluster together. Field isolation is absolute — blocking never
        # crosses fields (find_candidate_duplicate_beliefs filters field_id).
        fields = list_fields(conn, active_only=True)
        middle_pairs: list[_MiddlePair] = []
        confirmed_pairs: list[dict[str, str]] = []
        for fld in fields:
            embedded = ensure_belief_embeddings(conn, embedder, fld.id)
            candidates, total_held = load_candidate_beliefs(conn, fld.id)
            skipped = max(0, total_held - len(candidates))
            confirmed, middle = block_and_band(
                conn, embedder, candidates, config=cfg, field_id=fld.id
            )
            for pair in confirmed:
                a, b = sorted(pair)
                confirmed_pairs.append({"a_id": a, "b_id": b})
            for ep in middle.values():
                middle_pairs.append(
                    {
                        "field_id": fld.id,
                        "a_id": ep.a_id,
                        "b_id": ep.b_id,
                        "a_topic": ep.a.topic,
                        "a_statement": ep.a.statement,
                        "b_topic": ep.b.topic,
                        "b_statement": ep.b.statement,
                    }
                )
            log.info(
                "belief_candidates_loaded",
                field_id=fld.id, total_held=total_held, scanned=len(candidates),
                skipped=skipped, embedded=embedded,
                auto_merges=len(confirmed), middle=len(middle),
            )
        return {
            "confirmed_pairs": confirmed_pairs,
            "middle_pairs": middle_pairs,
            "fields_processed": len(fields),
        }

    def route_after_load(state: BeliefConsolidationState) -> str:
        if not state["middle_pairs"]:
            return "apply_merges"
        if batch_llm is not None:
            return "submit_batch"
        return "adjudicate_sync"

    async def adjudicate_sync(state: BeliefConsolidationState) -> dict[str, Any]:
        # Synchronous fallback (non-anthropic provider or batch disabled). With no
        # LLM at all the middle band defaults to not-same (conservative). Only
        # stages confirmed pairs; apply_merges does the actual folding.
        from mesh_agents.belief_consolidation import adjudicate_beliefs

        if sync_llm is None:
            return {}
        llm = sync_llm
        confirmed: list[dict[str, str]] = []
        for mp in state["middle_pairs"]:
            a = BeliefForMatch(topic=mp["a_topic"], statement=mp["a_statement"])
            b = BeliefForMatch(topic=mp["b_topic"], statement=mp["b_statement"])
            same = await asyncio.to_thread(adjudicate_beliefs, llm, a, b)
            if same:
                confirmed.append({"a_id": mp["a_id"], "b_id": mp["b_id"]})
        return {"confirmed_pairs": confirmed}

    async def submit_batch(state: BeliefConsolidationState) -> dict[str, Any]:
        assert batch_llm is not None
        from mesh_agents.belief_consolidation import (
            build_belief_adjudication_batch_items,
        )

        items = build_belief_adjudication_batch_items(
            [
                (
                    f"{mp['a_id']}|{mp['b_id']}",
                    BeliefForMatch(topic=mp["a_topic"], statement=mp["a_statement"]),
                    BeliefForMatch(topic=mp["b_topic"], statement=mp["b_statement"]),
                )
                for mp in state["middle_pairs"]
            ]
        )
        if not items:
            return {"batch_id": None}
        batch_id = await asyncio.to_thread(
            batch_llm.submit_batch, items, BeliefMatchDecision
        )
        log.info("belief_batch_submitted", batch_id=batch_id, requests=len(items))
        return {"batch_id": batch_id}

    def route_after_submit(state: BeliefConsolidationState) -> str:
        return "poll_batch" if state.get("batch_id") else "apply_merges"

    async def poll_batch(state: BeliefConsolidationState) -> dict[str, Any]:
        assert batch_llm is not None
        batch_id = state["batch_id"]
        if not batch_id:
            return {}
        deadline = time.monotonic() + _batch_timeout_seconds()
        while True:
            status = await asyncio.to_thread(batch_llm.batch_status, batch_id)
            if status == "ended":
                log.info("belief_batch_ended", batch_id=batch_id)
                break
            if time.monotonic() > deadline:
                log.warning("belief_batch_timeout", batch_id=batch_id, status=status)
                break
            await asyncio.sleep(_batch_poll_seconds())
        return {}

    async def collect_results(state: BeliefConsolidationState) -> dict[str, Any]:
        assert batch_llm is not None
        batch_id = state["batch_id"]
        if not batch_id:
            return {}
        results = await asyncio.to_thread(
            batch_llm.collect_batch, batch_id, BeliefMatchDecision
        )
        confirmed: list[dict[str, str]] = []
        usages: list[dict[str, Any]] = []
        for mp in state["middle_pairs"]:
            cid = f"{mp['a_id']}|{mp['b_id']}"
            res = results.get(cid)
            if res is None or res.parsed is None:
                log.warning(
                    "belief_batch_item_failed",
                    pair=cid, error=res.error if res else "missing result",
                )
                continue
            parsed = cast(BeliefMatchDecision, res.parsed)
            # Trace the batched generation to Langfuse at the discounted rate.
            system, user = build_belief_adjudication_prompt(
                BeliefForMatch(topic=mp["a_topic"], statement=mp["a_statement"]),
                BeliefForMatch(topic=mp["b_topic"], statement=mp["b_statement"]),
            )
            cost = estimate_cost(res.model, res.usage, batch=True)
            trace_generation(
                name="adjudicate_belief_match",
                model=res.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                output=parsed.model_dump_json(),
                latency_ms=0,
                usage=res.usage.model_dump(),
                cost_usd=cost.total_cost,
                agent_name=_AGENT,
            )
            usages.append(_usage_row(res.usage, res.model, batch=True))
            if parsed.same_proposition:
                confirmed.append({"a_id": mp["a_id"], "b_id": mp["b_id"]})
        return {"confirmed_pairs": confirmed, "usages": usages}

    async def apply_merges(state: BeliefConsolidationState) -> dict[str, Any]:
        # Single fold of ALL confirmed pairs (high band + adjudicated middle),
        # clustered together so a belief in two pairs merges onto one canonical
        # exactly once — never twice across phases.
        merged = _apply_confirmed(conn, state["confirmed_pairs"], confidence_fn)
        return {"beliefs_merged": merged}

    async def decay(state: BeliefConsolidationState) -> dict[str, Any]:
        # LLM-free staleness pass across every active field (Phase 19e).
        decayed = archived = 0
        for fld in list_fields(conn, active_only=True):
            d, a = decay_and_archive(conn, field_id=fld.id)
            decayed += d
            archived += a
        log.info("belief_decay_archive", decayed=decayed, archived=archived)
        return {"beliefs_decayed": decayed, "beliefs_archived": archived}

    async def finalize(state: BeliefConsolidationState) -> dict[str, Any]:
        if pipeline_run_exists(conn, state["run_id"]):
            log.info("finalize_already_done", run_id=state["run_id"])
            return {"finalized": True}
        for u in state["usages"]:
            usage = LLMUsage(**(u.get("usage") or {}))
            if usage.total_tokens == 0:
                continue
            model = str(u.get("model") or "")
            create_llm_usage(
                conn,
                LLMUsageRecord(
                    run_id=state["run_id"],
                    agent_name=_AGENT,
                    skill_id="adjudicate_belief_match",
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
            run_type="belief_consolidation",
            started_at=datetime.fromisoformat(state["started_at"]),
            finished_at=datetime.now(UTC),
            triggered_by=state["triggered_by"],
            beliefs_revised=state["beliefs_merged"]
            + state["beliefs_decayed"]
            + state["beliefs_archived"],
        )
        create_pipeline_run(conn, run)
        log.info(
            "belief_consolidation_complete",
            run_id=run.id,
            fields_processed=state["fields_processed"],
            beliefs_merged=state["beliefs_merged"],
            beliefs_decayed=state["beliefs_decayed"],
            beliefs_archived=state["beliefs_archived"],
        )
        return {"finalized": True}

    g: StateGraph[BeliefConsolidationState, Any, Any, Any] = StateGraph(
        BeliefConsolidationState
    )
    g.add_node("load_candidates", load_candidates)
    g.add_node("adjudicate_sync", adjudicate_sync)
    g.add_node("submit_batch", submit_batch)
    g.add_node("poll_batch", poll_batch)
    g.add_node("collect_results", collect_results)
    g.add_node("apply_merges", apply_merges)
    g.add_node("decay", decay)
    g.add_node("finalize", finalize)

    g.add_edge(START, "load_candidates")
    g.add_conditional_edges(
        "load_candidates", route_after_load,
        ["submit_batch", "adjudicate_sync", "apply_merges"],
    )
    g.add_edge("adjudicate_sync", "apply_merges")
    g.add_conditional_edges(
        "submit_batch", route_after_submit, ["poll_batch", "apply_merges"]
    )
    g.add_edge("poll_batch", "collect_results")
    g.add_edge("collect_results", "apply_merges")
    g.add_edge("apply_merges", "decay")
    g.add_edge("decay", "finalize")
    g.add_edge("finalize", END)
    return g


def _make_embedder() -> Any:
    from mesh_llm import make_embedder

    return make_embedder()


def _result_from_state(
    state: BeliefConsolidationState,
) -> BeliefConsolidationRunResult:
    return BeliefConsolidationRunResult(
        run_id=state["run_id"],
        fields_processed=state["fields_processed"],
        beliefs_merged=state["beliefs_merged"],
        beliefs_decayed=state["beliefs_decayed"],
        beliefs_archived=state["beliefs_archived"],
    )


async def run_belief_consolidation(
    db_path: str | None = None,
) -> BeliefConsolidationRunResult:
    """Top-level entry point — the ``mesh-consolidate-beliefs`` console script
    calls this. Iterates all active fields internally (no ``--field`` flag); the
    CLI ``consolidate-beliefs`` is the per-field, dry-run-capable variant."""
    log.info("belief_consolidation_starting")

    conn = get_connection(db_path)
    init_pg()

    run_id = os.environ.get("MESH_RUN_ID") or str(uuid.uuid4())
    initial_state: BeliefConsolidationState = {
        "run_id": run_id,
        "triggered_by": os.environ.get("MESH_TRIGGERED_BY", "manual"),
        "traceparent": new_traceparent(),
        "started_at": datetime.now(UTC).isoformat(),
        "confirmed_pairs": [],
        "middle_pairs": [],
        "batch_id": None,
        "fields_processed": 0,
        "beliefs_merged": 0,
        "beliefs_decayed": 0,
        "beliefs_archived": 0,
        "usages": [],
        "errors": [],
        "finalized": False,
    }

    # Batch path is on by default but only for the anthropic provider; otherwise
    # fall back to the synchronous LLM (or no adjudication). Model is env-routed
    # for the belief_consolidator role (MESH_LLM_MODEL_BELIEF_CONSOLIDATOR → default).
    batch_llm: AnthropicClient | None = None
    sync_llm: Any | None = None
    try:
        candidate = make_llm_client(agent_name=_AGENT)
    except LLMProviderNotReadyError as exc:
        log.info("belief_consolidation_provider_unavailable", error=str(exc))
        candidate = None
    if candidate is not None:
        if _batch_enabled() and isinstance(candidate, AnthropicClient):
            batch_llm = candidate
            log.info("belief_consolidation_batch_mode", model=batch_llm.model)
        else:
            sync_llm = candidate

    graph = build_belief_consolidation_graph(conn, batch_llm, sync_llm)
    async with open_checkpointer() as saver:
        app = graph.compile(checkpointer=saver)
        final = await app.ainvoke(initial_state, config=thread_config(run_id))
    conn.close()
    return _result_from_state(cast(BeliefConsolidationState, final))


def main() -> None:
    """Console-script entry point: ``uv run mesh-consolidate-beliefs``."""
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ]
    )
    db_path = os.environ.get("MESH_DB_PATH")
    result = asyncio.run(run_belief_consolidation(db_path=db_path))
    print(f"\nBelief consolidation {result.run_id}")
    print(f"  Fields processed:  {result.fields_processed}")
    print(f"  Beliefs merged:    {result.beliefs_merged}")
    print(f"  Beliefs decayed:   {result.beliefs_decayed}")
    print(f"  Beliefs archived:  {result.beliefs_archived}")
