"""Discovery sweep as a LangGraph graph (Phase 22d).

The proactive, whole-field counterpart to the reactive Curator. Per active field
it analyzes the knowledge state for gaps/trends (rule-based), drafts testable
hypotheses (one LLM pass), opens ``origin='discovery'`` Investigations, and
dispatches real hypothesis-directed search — reusing the coordinator's
investigate → extract → resolve → synthesize path so new knowledge still flows
only through the normal pipeline. Bounded (``MESH_DISCOVER_MAX_NEW`` /
``MESH_DISCOVER_MAX_FETCH``), idempotent (``pipeline_run_exists`` finalize guard),
field-scoped, and provenance-stamped.

Graph shape (one field per invocation; the scheduled job loops active fields)::

    START → plan ─[opened?]→ dispatch | finalize
      dispatch → finalize → END

State is checkpointed with thread_id == run_id (Postgres in docker, in-memory
locally / tests), exactly like the skeptic sweep + consolidation jobs.
"""
from __future__ import annotations

import asyncio
import operator
import os
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, TypedDict, cast

import click
import structlog
from langgraph.graph import END, START, StateGraph
from mesh_a2a.checkpoint import open_checkpointer, thread_config
from mesh_a2a.client import MeshA2AClient
from mesh_a2a.tracing import new_traceparent
from mesh_agents.connector import investigate_source_name
from mesh_agents.discovery import (
    DiscoveryProposal,
    GapSignal,
    analyze_field,
    build_discovery_investigations,
    draft_hypotheses,
)
from mesh_agents.entity_resolution import ResolutionConfig
from mesh_agents.profiles import load_profile
from mesh_db.connection import get_connection
from mesh_db.connectors import list_field_connectors
from mesh_db.fields import get_field_by_slug, list_fields
from mesh_db.investigations import create_investigation, get_investigation_by_id
from mesh_db.llm_usage import LLMUsageRecord, create_llm_usage
from mesh_db.pg_migrations import init_pg
from mesh_db.pipeline_runs import (
    PipelineRun,
    create_pipeline_run,
    pipeline_run_exists,
)
from mesh_llm import LLMClient, LLMProviderNotReadyError, LLMUsage, make_routed_llm_client
from mesh_llm.pricing import estimate_cost
from mesh_models.field import DEFAULT_FIELD_ID, DEFAULT_FIELD_SLUG
from mesh_models.investigation import Investigation
from pydantic import BaseModel

from mesh_pipeline.coordinator import (
    _agent_urls,
    _get_concurrency,
    _make_resolution_deps,
    _open_investigations,
    dispatch_open_investigations,
)

log = structlog.get_logger()


class DiscoveryResult(BaseModel):
    run_id: str
    field_slug: str
    gaps_found: int
    hypotheses_drafted: int
    investigations_opened: int
    fetches_dispatched: int
    claims_inserted: int


# ── env knobs ────────────────────────────────────────────────────────────────


def _max_new() -> int:
    return int(os.environ.get("MESH_DISCOVER_MAX_NEW", "5"))


def _max_fetch() -> int:
    return int(os.environ.get("MESH_DISCOVER_MAX_FETCH", "10"))


def _gap_limit() -> int:
    return int(os.environ.get("MESH_DISCOVER_GAP_LIMIT", "20"))


# ── shared planning (reads + LLM, no writes) ─────────────────────────────────


def _allowed_source_types(conn: Any, field_id: str) -> list[str]:
    """The investigate sources a field's enabled connectors back — the set
    discovery may suggest (and the dispatch will honour)."""
    return sorted(
        {
            investigate_source_name(fc.connector_id)
            for fc in list_field_connectors(conn, field_id, enabled_only=True)
        }
    )


def plan_field_discovery(
    conn: Any,
    llm: LLMClient | None,
    field_id: str,
    *,
    gap_limit: int,
    max_new: int,
) -> tuple[
    list[GapSignal], list[DiscoveryProposal], list[Investigation], LLMUsage | None, str
]:
    """Analyze → draft → build (deduped, capped) WITHOUT writing anything.

    Returns ``(gaps, proposals, investigations, draft_usage, draft_model)``. The
    graph's plan node persists the investigations; the CLI dry-run just reports
    them. With no LLM (provider unavailable / dry-run analyze-only) drafting is
    skipped and no investigations are built."""
    gaps = analyze_field(conn, field_id, limit=gap_limit)
    if llm is None or not gaps:
        return gaps, [], [], None, ""
    profile = load_profile(field_id)
    allowed = _allowed_source_types(conn, field_id)
    proposals, usage, model = draft_hypotheses(
        profile, gaps, llm=llm, allowed_source_types=allowed
    )
    existing = _open_investigations(conn, field_id=field_id)
    built = build_discovery_investigations(gaps, proposals, existing)[:max_new]
    return gaps, proposals, built, usage, model


# ── graph state ──────────────────────────────────────────────────────────────


class DiscoverState(TypedDict):
    run_id: str
    field_id: str
    field_slug: str
    triggered_by: str
    traceparent: str
    started_at: str  # ISO-8601
    gaps_found: int
    hypotheses_drafted: int
    opened_investigation_ids: list[str]
    investigations_opened: int
    draft_usage: dict[str, Any] | None
    draft_model: str
    dispatch: dict[str, Any]
    errors: Annotated[list[dict[str, Any]], operator.add]
    finalized: bool


# ── graph construction ───────────────────────────────────────────────────────


def build_discovery_graph(
    client: MeshA2AClient,
    conn: Any,
    *,
    draft_llm: LLMClient | None,
    embedder: Any,
    resolution_llm: LLMClient | None,
    semaphore: asyncio.Semaphore,
    resolution_config: ResolutionConfig,
) -> StateGraph[DiscoverState, Any, Any, Any]:
    async def plan(state: DiscoverState) -> dict[str, Any]:
        field_id = state["field_id"]
        # draft_hypotheses makes a blocking LLM call — keep the loop free.
        gaps, proposals, built, usage, model = await asyncio.to_thread(
            plan_field_discovery,
            conn,
            draft_llm,
            field_id,
            gap_limit=_gap_limit(),
            max_new=_max_new(),
        )
        opened_ids: list[str] = []
        for inv in built:
            create_investigation(conn, inv, field_id=field_id)
            opened_ids.append(inv.id)
        log.info(
            "discovery_planned",
            field=state["field_slug"],
            gaps=len(gaps),
            hypotheses=len(proposals),
            opened=len(opened_ids),
            capped_at=_max_new(),
        )
        return {
            "gaps_found": len(gaps),
            "hypotheses_drafted": len(proposals),
            "opened_investigation_ids": opened_ids,
            "investigations_opened": len(opened_ids),
            "draft_usage": usage.model_dump() if usage is not None else None,
            "draft_model": model,
        }

    def route_after_plan(state: DiscoverState) -> str:
        return "dispatch" if state["opened_investigation_ids"] else "finalize"

    async def dispatch(state: DiscoverState) -> dict[str, Any]:
        field_id = state["field_id"]
        investigations = [
            inv
            for inv in (
                get_investigation_by_id(conn, i)
                for i in state["opened_investigation_ids"]
            )
            if inv is not None
        ]
        if not investigations:
            return {}
        # Discover the live scout/extractor agents so skill_map() is populated
        # before dispatch (the coordinator does this in its scout node).
        await client.discover(_agent_urls())
        summary = await dispatch_open_investigations(
            client=client,
            conn=conn,
            embedder=embedder,
            llm=resolution_llm,
            semaphore=semaphore,
            resolution_config=resolution_config,
            field_id=field_id,
            traceparent=state["traceparent"],
            run_id=state["run_id"],
            investigations=investigations,
            max_fetch=_max_fetch(),
        )
        patch: dict[str, Any] = {"dispatch": summary}
        if summary.get("errors"):
            patch["errors"] = summary["errors"]
        return patch

    async def finalize(state: DiscoverState) -> dict[str, Any]:
        # Idempotency guard: a checkpointed graph can re-tick the final superstep.
        if pipeline_run_exists(conn, state["run_id"]):
            log.info("finalize_already_done", run_id=state["run_id"])
            return {"finalized": True}

        # Ledger the hypothesis-drafting LLM call (the client already traced the
        # generation to Langfuse; this is the cost ledger row).
        usage_raw = state.get("draft_usage")
        if usage_raw:
            usage = LLMUsage(**usage_raw)
            model = state.get("draft_model") or ""
            if usage.total_tokens:
                create_llm_usage(
                    conn,
                    LLMUsageRecord(
                        run_id=state["run_id"],
                        agent_name="discovery",
                        skill_id="draft_hypotheses",
                        model=model or None,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cache_read_tokens=usage.cache_read_tokens,
                        cache_creation_tokens=usage.cache_creation_tokens,
                        estimated_cost_usd=estimate_cost(
                            model, usage, batch=False
                        ).total_cost,
                    ),
                )

        dispatch_summary = state.get("dispatch") or {}
        claims = int(dispatch_summary.get("investigation_claims_inserted", 0))
        run = PipelineRun(
            id=state["run_id"],
            run_type="discovery",
            started_at=datetime.fromisoformat(state["started_at"]),
            finished_at=datetime.now(UTC),
            triggered_by=state["triggered_by"],
            claims_inserted=claims,
            beliefs_revised=0,
            sources_inserted=int(dispatch_summary.get("investigations_dispatched", 0)),
        )
        create_pipeline_run(conn, run, field_id=state["field_id"])
        log.info(
            "discovery_complete",
            run_id=run.id,
            field=state["field_slug"],
            gaps_found=state["gaps_found"],
            hypotheses_drafted=state["hypotheses_drafted"],
            investigations_opened=state["investigations_opened"],
            fetches_dispatched=int(dispatch_summary.get("investigations_dispatched", 0)),
            claims_inserted=claims,
        )
        return {"finalized": True}

    g: StateGraph[DiscoverState, Any, Any, Any] = StateGraph(DiscoverState)
    g.add_node("plan", plan)
    g.add_node("dispatch", dispatch)
    g.add_node("finalize", finalize)
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", route_after_plan, ["dispatch", "finalize"])
    g.add_edge("dispatch", "finalize")
    g.add_edge("finalize", END)
    return g


def _result_from_state(state: DiscoverState) -> DiscoveryResult:
    dispatch_summary = state.get("dispatch") or {}
    return DiscoveryResult(
        run_id=state["run_id"],
        field_slug=state["field_slug"],
        gaps_found=state["gaps_found"],
        hypotheses_drafted=state["hypotheses_drafted"],
        investigations_opened=state["investigations_opened"],
        fetches_dispatched=int(dispatch_summary.get("investigations_dispatched", 0)),
        claims_inserted=int(dispatch_summary.get("investigation_claims_inserted", 0)),
    )


async def run_discovery(
    field: str = DEFAULT_FIELD_SLUG, *, conn: Any | None = None
) -> DiscoveryResult:
    """Run one field's discovery sweep end-to-end (open + dispatch). ``field`` is
    a slug; an unknown slug falls back to the default field id."""
    log.info("discovery_starting", field=field)
    owns_conn = conn is None
    if conn is None:
        conn = get_connection()
        init_pg()

    field_row = get_field_by_slug(conn, field)
    field_id = field_row.id if field_row is not None else DEFAULT_FIELD_ID

    run_id = os.environ.get("MESH_RUN_ID") or str(uuid.uuid4())
    initial_state: DiscoverState = {
        "run_id": run_id,
        "field_id": field_id,
        "field_slug": field,
        "triggered_by": os.environ.get("MESH_TRIGGERED_BY", "manual"),
        "traceparent": new_traceparent(),
        "started_at": datetime.now(UTC).isoformat(),
        "gaps_found": 0,
        "hypotheses_drafted": 0,
        "opened_investigation_ids": [],
        "investigations_opened": 0,
        "draft_usage": None,
        "draft_model": "",
        "dispatch": {},
        "errors": [],
        "finalized": False,
    }

    draft_llm: LLMClient | None = None
    try:
        draft_llm = make_routed_llm_client(agent_name="discovery")
    except LLMProviderNotReadyError as exc:
        log.info("discovery_provider_unavailable", error=str(exc))

    embedder, resolution_llm = _make_resolution_deps()
    semaphore = asyncio.Semaphore(_get_concurrency())
    resolution_config = ResolutionConfig.from_env()

    async with MeshA2AClient() as client:
        graph = build_discovery_graph(
            client,
            conn,
            draft_llm=draft_llm,
            embedder=embedder,
            resolution_llm=resolution_llm,
            semaphore=semaphore,
            resolution_config=resolution_config,
        )
        async with open_checkpointer() as saver:
            app = graph.compile(checkpointer=saver)
            final = await app.ainvoke(initial_state, config=thread_config(run_id))
    if owns_conn:
        conn.close()
    return _result_from_state(cast(DiscoverState, final))


async def run_discovery_all() -> list[DiscoveryResult]:
    """Sweep every active field (the scheduled job's no-field default)."""
    conn = get_connection()
    init_pg()
    try:
        fields = list_fields(conn, active_only=True)
    finally:
        conn.close()
    results: list[DiscoveryResult] = []
    for f in fields:
        results.append(await run_discovery(f.slug))
    return results


@click.command()
@click.option(
    "--field",
    default=os.environ.get("MESH_DISCOVER_FIELD"),
    help="Field slug to scope this sweep to. Omit to sweep all active fields.",
)
def main(field: str | None) -> None:
    """Console-script entry point: `uv run mesh-discover`."""
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ]
    )
    results = (
        [asyncio.run(run_discovery(field))]
        if field
        else asyncio.run(run_discovery_all())
    )
    for r in results:
        print(f"\nDiscovery {r.run_id} [{r.field_slug}]")
        print(f"  Gaps found:            {r.gaps_found}")
        print(f"  Hypotheses drafted:    {r.hypotheses_drafted}")
        print(f"  Investigations opened: {r.investigations_opened}")
        print(f"  Fetches dispatched:    {r.fetches_dispatched}")
        print(f"  Claims inserted:       {r.claims_inserted}")
