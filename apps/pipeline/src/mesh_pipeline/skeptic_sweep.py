"""Skeptic sweep as a LangGraph graph (Phase 8).

Out-of-band falsification orchestrator. Curator ranks which held beliefs
deserve a challenge, the Skeptic assesses each, and assessments clearing
the apply-threshold write counter-claims + a BeliefRevision.

Graph shape::

    START → load_beliefs ─[picks?]→ evaluate_one (Send fan-out) | finalize
      evaluate_one ─[any contradicted?]→ trigger_curator | finalize
      trigger_curator → finalize → END

Distinct from the coordinator on purpose: the falsification flow touches
only beliefs + revisions, never scout/extract/synthesis. State is
checkpointed with thread_id == run_id (Postgres in docker, in-memory
locally / tests).

Persistence note: the per-belief assessment is written inside the
``evaluate_one`` fan-out worker. DuckDB writes are synchronous and never
await, so the single-threaded event loop serializes them across the
concurrent workers — no concurrent-transaction hazard, and each worker
operates on a distinct belief.
"""
from __future__ import annotations

import asyncio
import hashlib
import operator
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, TypedDict, cast

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from mesh_a2a.checkpoint import open_checkpointer, thread_config
from mesh_a2a.client import MeshA2AClient
from mesh_a2a.node import call_skill_node
from mesh_a2a.tracing import new_traceparent
from mesh_agents.curator import BeliefForCuration, CuratorPick, InvestigationSuggestion
from mesh_agents.skeptic import (
    HydratedClaim,
    InScopeEntity,
    SkepticAssessment,
    SkepticCounterClaim,
)
from mesh_agents.sota_tracker import BeliefSummary
from mesh_db.beliefs import get_belief_by_id, list_beliefs, update_belief
from mesh_db.claims import create_claim, get_claims_by_ids
from mesh_db.connection import get_connection
from mesh_db.entities import get_entity_by_id
from mesh_db.llm_usage import LLMUsageRecord, create_llm_usage
from mesh_db.migrations import apply_migrations
from mesh_db.pipeline_runs import PipelineRun, create_pipeline_run
from mesh_db.revisions import create_revision, list_revisions
from mesh_db.sources import create_source, get_source_by_id
from mesh_llm.pricing import estimate_cost
from mesh_llm.usage import LLMUsage
from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.revision import BeliefRevision
from mesh_models.source import Source, SourceType
from pydantic import BaseModel

from mesh_pipeline._investigations import persist_investigation_suggestions

log = structlog.get_logger()

_DEFAULT_AGENT_URLS = [
    "http://curator:8007",
    "http://skeptic:8006",
]


class SkepticSweepResult(BaseModel):
    run_id: str
    beliefs_considered: int
    beliefs_picked: int
    assessments_run: int
    assessments_applied: int
    counter_claims_inserted: int
    revisions_inserted: int


# ── env knobs ────────────────────────────────────────────────────────────────


def _agent_urls() -> list[str]:
    raw = os.environ.get("MESH_SKEPTIC_AGENT_URLS", "")
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]
    return _DEFAULT_AGENT_URLS


def _apply_threshold() -> float:
    return float(os.environ.get("MESH_SKEPTIC_APPLY_THRESHOLD", "0.7"))


def _pick_count() -> int:
    return int(os.environ.get("MESH_CURATOR_PICK_COUNT", "5"))


def _cooldown_days() -> int:
    return int(os.environ.get("MESH_CURATOR_COOLDOWN_DAYS", "7"))


def _source_reliability() -> float:
    return float(os.environ.get("MESH_SKEPTIC_SOURCE_RELIABILITY", "0.4"))


# ── curator payload helpers ──────────────────────────────────────────────────


def _last_skeptic_challenge(revisions: list[BeliefRevision]) -> datetime | None:
    skeptic_revs = [r for r in revisions if r.revised_by_agent == "skeptic"]
    if not skeptic_revs:
        return None
    return max(r.revised_at for r in skeptic_revs)


def _recent_contradicting_activity(
    revisions: list[BeliefRevision], now: datetime, window_days: int = 14
) -> bool:
    cutoff = now - timedelta(days=window_days)
    for r in revisions:
        if r.revised_at < cutoff:
            continue
        if r.new_confidence < r.previous_confidence:
            return True
    return False


def _last_evidence_at(conn: Any, belief: Belief) -> datetime | None:
    """Most recent extracted_at across the belief's supporting + contradicting claims."""
    ids = list(belief.supporting_claim_ids) + list(belief.contradicting_claim_ids)
    if not ids:
        return None
    claims = get_claims_by_ids(conn, ids)
    if not claims:
        return None
    return max(c.extracted_at for c in claims)


def _build_curator_payload(
    conn: Any, beliefs: list[Belief], now: datetime
) -> list[BeliefForCuration]:
    payload: list[BeliefForCuration] = []
    for b in beliefs:
        revs = list_revisions(conn, belief_id=b.id, limit=200)
        payload.append(
            BeliefForCuration(
                belief_id=b.id,
                topic=b.topic,
                statement=b.statement,
                confidence=b.confidence,
                supporting_claim_count=len(b.supporting_claim_ids),
                contradicting_claim_count=len(b.contradicting_claim_ids),
                last_revised_at=b.last_revised_at,
                last_challenged_at=_last_skeptic_challenge(revs),
                recent_contradicting_activity=_recent_contradicting_activity(revs, now),
                last_evidence_at=_last_evidence_at(conn, b),
            )
        )
    return payload


async def _select_beliefs(
    client: MeshA2AClient, conn: Any, traceparent: str
) -> tuple[list[CuratorPick], int, dict[str, Any] | None]:
    """Dispatch the Curator over held beliefs. Returns (picks, n_investigations,
    error). Persists any investigation suggestions as a side effect."""
    held = list_beliefs(conn, currently_held=True, limit=1000)
    now = datetime.now(UTC)
    payload = _build_curator_payload(conn, held, now)
    result, err = await call_skill_node(
        client,
        "select_beliefs_to_challenge",
        {
            "beliefs": [b.model_dump(mode="json") for b in payload],
            "pick_count": _pick_count(),
            "now": now.isoformat(),
            "cooldown_days": _cooldown_days(),
        },
        traceparent=traceparent,
    )
    if result is None:
        return [], 0, err.model_dump() if err is not None else None
    picks = [CuratorPick.model_validate(p) for p in result.get("picks", [])]
    suggestions = [
        InvestigationSuggestion.model_validate(s)
        for s in result.get("investigation_suggestions", [])
    ]
    n_inv = persist_investigation_suggestions(conn, suggestions)
    return picks, n_inv, None


# ── skeptic hydration + persistence helpers ──────────────────────────────────


def _hydrate_claims(conn: Any, ids: list[str]) -> list[HydratedClaim]:
    if not ids:
        return []
    claims = get_claims_by_ids(conn, ids)
    out: list[HydratedClaim] = []
    for c in claims:
        source = get_source_by_id(conn, c.source_id)
        out.append(
            HydratedClaim(
                claim_id=c.id,
                predicate=c.predicate,
                subject_entity_id=c.subject_entity_id,
                object=c.object,
                raw_excerpt=c.raw_excerpt,
                confidence=c.confidence,
                source_url=source.url if source else None,
                source_published_at=source.published_at if source else None,
                source_reliability=source.reliability_prior if source else None,
                extracted_at=c.extracted_at,
                status=c.status.value,
            )
        )
    return out


def _collect_in_scope_entities(
    conn: Any, supporting: list[HydratedClaim], contradicting: list[HydratedClaim]
) -> list[InScopeEntity]:
    ids: set[str] = set()
    for c in supporting + contradicting:
        ids.add(c.subject_entity_id)
    out: list[InScopeEntity] = []
    for eid in ids:
        ent = get_entity_by_id(conn, eid)
        if ent is None:
            continue
        out.append(
            InScopeEntity(
                entity_id=ent.id,
                canonical_name=ent.canonical_name,
                type=ent.type.value,
            )
        )
    return out


def _make_skeptic_source(belief_id: str, rationale: str, now: datetime) -> Source:
    iso = now.strftime("%Y%m%dT%H%M%SZ")
    return Source(
        type=SourceType.agent_reasoning,
        url=f"agent://skeptic/belief/{belief_id}/{iso}",
        author="skeptic",
        published_at=now,
        raw_content_hash=hashlib.sha256(
            f"{belief_id}|{now.isoformat()}|{rationale}".encode()
        ).hexdigest(),
        reliability_prior=_source_reliability(),
    )


def _counter_to_claim(cc: SkepticCounterClaim, source_id: str) -> Claim:
    return Claim(
        predicate=cc.predicate,
        subject_entity_id=cc.subject_entity_id,
        object=cc.object,
        source_id=source_id,
        extracted_by_agent="skeptic",
        raw_excerpt=cc.raw_excerpt,
        confidence=cc.confidence,
        failure_mode=cc.failure_mode,
    )


def _persist_assessment(
    conn: Any, belief: Belief, assessment: SkepticAssessment, now: datetime
) -> tuple[int, int]:
    """Insert source + counter-claims + revision, update belief. Returns
    (n_claims, n_revisions)."""
    if not assessment.counter_claims:
        # No counter-claims = no trigger evidence; skip writing a phantom revision.
        return (0, 0)

    source = _make_skeptic_source(belief.id, assessment.rationale, now)
    create_source(conn, source)

    new_claim_ids: list[str] = []
    for cc in assessment.counter_claims:
        claim = _counter_to_claim(cc, source.id)
        create_claim(conn, claim)
        new_claim_ids.append(claim.id)

    # Update the belief FIRST, append the revision second — DuckDB rejects an
    # UPDATE on a row already referenced by a freshly-inserted row in the same
    # tx, so this ordering sidesteps that quirk.
    new_confidence = max(
        0.0, min(1.0, belief.confidence + assessment.suggested_confidence_delta)
    )
    revision = BeliefRevision(
        belief_id=belief.id,
        previous_statement=belief.statement,
        new_statement=belief.statement,  # skeptic does not rewrite the statement
        previous_confidence=belief.confidence,
        new_confidence=new_confidence,
        trigger_claim_ids=new_claim_ids,
        revised_by_agent="skeptic",
        rationale=assessment.rationale,
    )

    update_fields: dict[str, Any] = {
        "confidence": new_confidence,
        "last_revised_at": revision.revised_at,
        "revision_count": belief.revision_count + 1,
    }
    if assessment.verdict == "contradicted":
        update_fields["contradicting_claim_ids"] = (
            list(belief.contradicting_claim_ids) + new_claim_ids
        )
    update_belief(conn, belief.id, **update_fields)
    create_revision(conn, revision)
    return (len(new_claim_ids), 1)


# ── graph state ──────────────────────────────────────────────────────────────


class SweepState(TypedDict):
    run_id: str
    triggered_by: str
    traceparent: str
    started_at: str  # ISO-8601
    beliefs_to_evaluate: list[str]
    verdicts: Annotated[list[dict[str, Any]], operator.add]
    curator_triggered: bool
    beliefs_considered: int
    beliefs_picked: int
    investigations_opened: int
    errors: Annotated[list[dict[str, Any]], operator.add]
    finalized: bool


class _BeliefWork(TypedDict):
    """Per-belief payload delivered to the evaluate_one fan-out worker via Send."""

    belief_id: str
    traceparent: str


# ── graph construction ───────────────────────────────────────────────────────


def build_sweep_graph(
    client: MeshA2AClient, conn: Any
) -> StateGraph[SweepState, Any, Any, Any]:
    """Build the skeptic-sweep graph. Nodes close over the live A2A client +
    DuckDB connection."""
    threshold = _apply_threshold()

    async def load_beliefs(state: SweepState) -> dict[str, Any]:
        held = list_beliefs(conn, currently_held=True, limit=1000)
        log.info("beliefs_considered", count=len(held))
        if not held:
            return {"beliefs_considered": 0, "beliefs_picked": 0}

        discovered = await client.discover(_agent_urls())
        for required in ("select_beliefs_to_challenge", "challenge_belief"):
            if required not in discovered:
                raise SystemExit(
                    f"Required skill '{required}' not discovered. "
                    f"Discovered: {list(discovered.keys())}"
                )

        picks, n_inv, err = await _select_beliefs(client, conn, state["traceparent"])
        patch: dict[str, Any] = {
            "beliefs_to_evaluate": [p.belief_id for p in picks],
            "beliefs_considered": len(held),
            "beliefs_picked": len(picks),
            "investigations_opened": n_inv,
        }
        if err is not None:
            patch["errors"] = [err]
        log.info("beliefs_picked", count=len(picks), investigations_opened=n_inv)
        return patch

    def route_after_load(state: SweepState) -> list[Send] | str:
        if not state["beliefs_to_evaluate"]:
            return "finalize"
        tp = state["traceparent"]
        return [
            Send("evaluate_one", {"belief_id": bid, "traceparent": tp})
            for bid in state["beliefs_to_evaluate"]
        ]

    async def evaluate_one(state: _BeliefWork) -> dict[str, Any]:
        belief = get_belief_by_id(conn, state["belief_id"])
        if belief is None:
            log.warning("picked_belief_missing", belief_id=state["belief_id"])
            return {}
        supporting = _hydrate_claims(conn, belief.supporting_claim_ids)
        contradicting = _hydrate_claims(conn, belief.contradicting_claim_ids)
        in_scope = _collect_in_scope_entities(conn, supporting, contradicting)

        result, err = await call_skill_node(
            client,
            "challenge_belief",
            {
                "belief": BeliefSummary(
                    belief_id=belief.id,
                    topic=belief.topic,
                    statement=belief.statement,
                    confidence=belief.confidence,
                ).model_dump(mode="json"),
                "supporting_claims": [c.model_dump(mode="json") for c in supporting],
                "contradicting_claims": [c.model_dump(mode="json") for c in contradicting],
                "in_scope_entities": [e.model_dump(mode="json") for e in in_scope],
            },
            traceparent=state["traceparent"],
            context={"belief_id": belief.id},
        )
        if result is None:
            return {"errors": [err.model_dump()] if err is not None else []}

        assessment = SkepticAssessment(
            verdict=result["verdict"],
            confidence=float(result["confidence"]),
            rationale=result["rationale"],
            suggested_confidence_delta=float(result.get("suggested_confidence_delta", 0.0)),
            counter_claims=[
                SkepticCounterClaim.model_validate(c)
                for c in result.get("counter_claims", [])
            ],
        )
        log.info(
            "skeptic_assessment",
            belief_id=belief.id,
            verdict=assessment.verdict,
            confidence=assessment.confidence,
            counter_claim_count=len(assessment.counter_claims),
        )

        applied = False
        n_claims = n_revs = 0
        if (
            assessment.verdict in {"weakened", "contradicted"}
            and assessment.confidence >= threshold
        ):
            n_claims, n_revs = _persist_assessment(
                conn, belief, assessment, datetime.now(UTC)
            )
            applied = n_revs > 0
        return {
            "verdicts": [
                {
                    "belief_id": belief.id,
                    "verdict": assessment.verdict,
                    "confidence": assessment.confidence,
                    "applied": applied,
                    "n_counter_claims": n_claims,
                    "n_revisions": n_revs,
                    "usage": result.get("usage") or {},
                    "model": result.get("model") or "",
                }
            ]
        }

    def route_after_evaluate(state: SweepState) -> str:
        any_contradicted = any(v["verdict"] == "contradicted" for v in state["verdicts"])
        return "trigger_curator" if any_contradicted else "finalize"

    async def trigger_curator(state: SweepState) -> dict[str, Any]:
        # A contradiction landed — re-run the Curator over the now-updated
        # beliefs so it can open investigations for the freshly-weakened ones.
        _, n_inv, err = await _select_beliefs(client, conn, state["traceparent"])
        patch: dict[str, Any] = {
            "curator_triggered": True,
            "investigations_opened": state["investigations_opened"] + n_inv,
        }
        if err is not None:
            patch["errors"] = [err]
        log.info("curator_triggered", investigations_opened=n_inv)
        return patch

    async def finalize(state: SweepState) -> dict[str, Any]:
        verdicts = state["verdicts"]
        for v in verdicts:
            usage = LLMUsage(**(v.get("usage") or {}))
            if usage.total_tokens == 0:
                continue
            model = str(v.get("model") or "")
            create_llm_usage(
                conn,
                LLMUsageRecord(
                    run_id=state["run_id"],
                    agent_name="skeptic",
                    skill_id="challenge_belief",
                    model=model or None,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_creation_tokens=usage.cache_creation_tokens,
                    estimated_cost_usd=estimate_cost(model, usage).total_cost,
                ),
            )
        counter_claims = sum(int(v["n_counter_claims"]) for v in verdicts)
        revisions = sum(int(v["n_revisions"]) for v in verdicts)
        run = PipelineRun(
            id=state["run_id"],
            run_type="skeptic_sweep",
            started_at=datetime.fromisoformat(state["started_at"]),
            finished_at=datetime.now(UTC),
            triggered_by=state["triggered_by"],
            claims_inserted=counter_claims,
            beliefs_revised=revisions,
            sources_inserted=revisions,  # one synthetic source per applied assessment
        )
        create_pipeline_run(conn, run)
        log.info(
            "skeptic_sweep_complete",
            run_id=run.id,
            beliefs_considered=state["beliefs_considered"],
            beliefs_picked=state["beliefs_picked"],
            assessments_run=len(verdicts),
            assessments_applied=sum(1 for v in verdicts if v["applied"]),
            counter_claims_inserted=counter_claims,
            revisions_inserted=revisions,
        )
        return {"finalized": True}

    g: StateGraph[SweepState, Any, Any, Any] = StateGraph(SweepState)
    g.add_node("load_beliefs", load_beliefs)
    # Fan-out worker reads a per-belief Send payload, not the graph state.
    g.add_node("evaluate_one", evaluate_one, input_schema=_BeliefWork)
    g.add_node("trigger_curator", trigger_curator)
    g.add_node("finalize", finalize)

    g.add_edge(START, "load_beliefs")
    g.add_conditional_edges("load_beliefs", route_after_load, ["evaluate_one", "finalize"])
    g.add_conditional_edges(
        "evaluate_one", route_after_evaluate, ["trigger_curator", "finalize"]
    )
    g.add_edge("trigger_curator", "finalize")
    g.add_edge("finalize", END)
    return g


def _sweep_result(state: SweepState) -> SkepticSweepResult:
    verdicts = state["verdicts"]
    return SkepticSweepResult(
        run_id=state["run_id"],
        beliefs_considered=state["beliefs_considered"],
        beliefs_picked=state["beliefs_picked"],
        assessments_run=len(verdicts),
        assessments_applied=sum(1 for v in verdicts if v["applied"]),
        counter_claims_inserted=sum(int(v["n_counter_claims"]) for v in verdicts),
        revisions_inserted=sum(int(v["n_revisions"]) for v in verdicts),
    )


async def run_skeptic_sweep(db_path: str | None = None) -> SkepticSweepResult:
    """Top-level entry point — `mesh-skeptic-sweep` console script calls this."""
    log.info("skeptic_sweep_starting")

    conn = get_connection(db_path)
    apply_migrations(conn)

    # A manual trigger from the API/scheduler can pin the run id (so the
    # returned id matches this run's pipeline_runs row + checkpoint thread).
    run_id = os.environ.get("MESH_RUN_ID") or str(uuid.uuid4())
    initial_state: SweepState = {
        "run_id": run_id,
        "triggered_by": os.environ.get("MESH_TRIGGERED_BY", "manual"),
        "traceparent": new_traceparent(),
        "started_at": datetime.now(UTC).isoformat(),
        "beliefs_to_evaluate": [],
        "verdicts": [],
        "curator_triggered": False,
        "beliefs_considered": 0,
        "beliefs_picked": 0,
        "investigations_opened": 0,
        "errors": [],
        "finalized": False,
    }

    async with MeshA2AClient() as client:
        graph = build_sweep_graph(client, conn)
        async with open_checkpointer() as saver:
            app = graph.compile(checkpointer=saver)
            final = await app.ainvoke(initial_state, config=thread_config(run_id))
    conn.close()
    return _sweep_result(cast(SweepState, final))


def main() -> None:
    """Console-script entry point: `uv run mesh-skeptic-sweep`."""
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ]
    )
    db_path = os.environ.get("MESH_DB_PATH")
    result = asyncio.run(run_skeptic_sweep(db_path=db_path))
    print(f"\nSkeptic sweep {result.run_id}")
    print(f"  Beliefs considered: {result.beliefs_considered}")
    print(f"  Beliefs picked:     {result.beliefs_picked}")
    print(f"  Assessments run:    {result.assessments_run}")
    print(f"  Assessments applied:{result.assessments_applied}")
    print(f"  Counter-claims:     {result.counter_claims_inserted}")
    print(f"  Revisions:          {result.revisions_inserted}")
