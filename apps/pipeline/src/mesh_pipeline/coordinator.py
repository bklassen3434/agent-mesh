"""A2A coordinator as a LangGraph graph (Phase 8).

Replaces the fixed imperative DAG with a stateful LangGraph graph that
supports conditional routing, fan-out via ``Send``, and Postgres-backed
checkpointing. The agents are unchanged — still A2A servers discovered by
skill id. What changed is *how* they're orchestrated.

Graph shape::

    START → scout ─(Send fan-out)→ scout_one → ingest
      ingest ─[new papers?]→ extract_one (Send fan-out) | finalize
      extract_one ─[claims>0?]→ track_entities | finalize
      track_entities ─[resolved claims?]→ synthesize | curate
      synthesize → curate          (score + capability belief handlers)
      curate ─[open investigations?]→ dispatch_investigations | finalize
      dispatch_investigations → finalize → END

Error philosophy (preserved from the imperative coordinator): a single
skill failure is recorded into ``state["errors"]`` and the graph
continues — one bad paper never aborts the run.

DB ownership is unchanged: the coordinator owns every read/write; the
fan-out worker nodes touch only the network, so the shared Postgres
connection is only ever used from join/sequential nodes (no concurrent
access).
"""
from __future__ import annotations

import asyncio
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
from mesh_agents.arxiv_scout import ScoutedPaper
from mesh_agents.claim_extractor import ExtractedClaim
from mesh_agents.confidence import (
    BeliefSignals,
    ConfidenceWeights,
    compute_confidence,
)
from mesh_agents.connector import connector_skill_id, investigate_source_name
from mesh_agents.entity_resolution import ResolutionConfig, resolve_entity_semantic
from mesh_agents.entity_tracker import EntitySummary, ResolvedEntityInfo
from mesh_agents.sota_tracker import BeliefSummary, BeliefUpdate, ResolvedClaim
from mesh_agents.synthesis import (
    CAPABILITY_TOPIC_PREFIX,
    CapabilityBeliefInput,
    ExistingCapabilityBelief,
    capability_topic,
    edge_for_claim,
    synthesize_capability_belief,
)
from mesh_db.beliefs import (
    create_belief,
    get_belief_by_id,
    get_belief_signals,
    list_beliefs,
    set_belief_embedding,
    update_belief,
)
from mesh_db.claims import create_claim, list_claims
from mesh_db.connection import get_connection
from mesh_db.connectors import list_field_connectors
from mesh_db.entities import create_entity, get_entity_by_id, list_entities
from mesh_db.fields import get_field_by_slug
from mesh_db.investigations import (
    attach_claim_to_investigation,
    list_investigations,
    update_investigation,
)
from mesh_db.llm_usage import LLMUsageRecord, create_llm_usage
from mesh_db.pg_migrations import init_pg
from mesh_db.pipeline_runs import (
    PipelineError,
    PipelineRun,
    create_pipeline_run,
    pipeline_run_exists,
)
from mesh_db.processed_items import (
    ProcessedDecision,
    decide,
    record_processed_item,
    touch_processed_item,
)
from mesh_db.relationships import add_relationship_evidence
from mesh_db.revisions import create_revision
from mesh_db.sources import create_source, list_sources
from mesh_llm import Embedder, belief_embed_text
from mesh_llm.pricing import estimate_cost
from mesh_llm.protocol import LLMClient
from mesh_llm.usage import LLMUsage
from mesh_models.belief import Belief
from mesh_models.claim import Claim, ClaimStatus, ClaimType, claim_type_for_predicate
from mesh_models.entity import Entity, EntityType
from mesh_models.field import DEFAULT_FIELD_ID, DEFAULT_FIELD_SLUG
from mesh_models.investigation import InvestigationStatus
from mesh_models.revision import BeliefRevision
from pydantic import BaseModel

log = structlog.get_logger()

_DEFAULT_AGENT_URLS = [
    "http://arxiv-scout:8001",
    "http://claim-extractor:8002",
    "http://entity-tracker:8003",
    "http://sota-tracker:8004",
    "http://hn-scout:8005",
    "http://github-scout:8008",
    "http://bluesky-scout:8009",
    "http://reddit-scout:8010",
    "http://blog-scout:8011",
    "http://leaderboard-scout:8012",
]

_MODEL_LIKE_TYPES = {"model", "benchmark"}


class PipelineResult(BaseModel):
    run_id: str
    papers_scouted: int
    sources_inserted: int
    items_skipped: int = 0
    claims_inserted: int
    entities_created: int
    beliefs_created: int
    beliefs_revised: int
    relationships_created: int = 0
    avg_extraction_latency_ms: int
    errors: list[dict[str, str]]


# ── env knobs ────────────────────────────────────────────────────────────────


def _get_concurrency() -> int:
    return int(os.environ.get("MESH_PIPELINE_CONCURRENCY", "3"))


def _agent_urls() -> list[str]:
    raw = os.environ.get("MESH_AGENT_URLS", "")
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]
    return _DEFAULT_AGENT_URLS


def _investigation_claims_threshold() -> int:
    return int(os.environ.get("MESH_INVESTIGATION_CLAIMS_THRESHOLD", "3"))


def _investigation_max_runs() -> int:
    return int(os.environ.get("MESH_INVESTIGATION_MAX_RUNS", "5"))


# ── graph state ──────────────────────────────────────────────────────────────


class CoordinatorState(TypedDict):
    run_id: str
    field_id: str  # the field this run scopes all reads/writes to (Phase 17a)
    triggered_by: str
    traceparent: str
    started_at: str  # ISO-8601
    # Optional run-level override of the arxiv connector's categories (legacy
    # --categories flag). None → each connector uses its per-field config.
    categories: list[str] | None
    max_papers: int
    since: str | None
    # scouting: the run's enabled connectors → their per-field config (17c).
    scout_skill_ids: list[str]
    scout_configs: dict[str, dict[str, Any]]
    raw_papers: Annotated[list[dict[str, Any]], operator.add]
    # ingest
    new_papers: list[dict[str, Any]]
    papers_scouted: int
    sources_inserted: int
    items_skipped: int  # dedup: items skipped (already processed, unchanged)
    # extraction (fan-out workers append here): {paper, claims, latency_ms}
    extractions: Annotated[list[dict[str, Any]], operator.add]
    avg_extraction_latency_ms: int
    # entities + claims (main path)
    entities_created: int
    claims_inserted: int
    resolved_claims: list[dict[str, Any]]
    has_model_or_benchmark_entity: bool
    # synthesis
    beliefs_created: int
    beliefs_revised: int
    relationships_created: int
    # investigations (relocated full 7a lifecycle)
    has_open_investigations: bool
    investigations_dispatched: int
    investigations_resolved: int
    investigations_abandoned: int
    investigation_claims_inserted: int
    investigation_entities_created: int
    # cross-cutting
    errors: Annotated[list[dict[str, Any]], operator.add]
    finalized: bool


class _ScoutWork(TypedDict):
    """Per-scout payload delivered to the scout_one fan-out worker via Send."""

    skill_id: str
    payload: dict[str, Any]
    traceparent: str


class _ExtractWork(TypedDict):
    """Per-paper payload delivered to the extract_one fan-out worker via Send."""

    paper: dict[str, Any]
    traceparent: str
    field_id: str


# ── DB-side helpers (coordinator owns all reads/writes) ──────────────────────


def _dedup_and_insert_sources(
    conn: Any, papers: list[ScoutedPaper], *, field_id: str = DEFAULT_FIELD_ID
) -> list[ScoutedPaper]:
    """Drop papers whose source hash already exists (within this field), insert
    the rest scoped to ``field_id``."""
    existing_hashes: set[str] = {
        s.raw_content_hash for s in list_sources(conn, limit=10000, field_id=field_id)
    }
    new_papers = [
        p for p in papers if p.source.raw_content_hash not in existing_hashes
    ]
    # Guard against duplicate hashes *within* this batch too.
    seen: set[str] = set()
    deduped: list[ScoutedPaper] = []
    for p in new_papers:
        if p.source.raw_content_hash in seen:
            continue
        seen.add(p.source.raw_content_hash)
        deduped.append(p)
    for paper in deduped:
        create_source(conn, paper.source, field_id=field_id)
    return deduped


async def _resolve_entities(
    conn: Any,
    client: MeshA2AClient,
    names: list[str],
    traceparent: str,
    *,
    field_id: str = DEFAULT_FIELD_ID,
) -> list[ResolvedEntityInfo]:
    """Resolve candidate names via the resolve_entities skill, falling back to
    pure local resolution when the skill is absent or errors. The existing-entity
    context is scoped to ``field_id`` so resolution never sees another field."""
    if not names:
        return []
    existing = [
        EntitySummary(
            entity_id=e.id,
            canonical_name=e.canonical_name,
            aliases=e.aliases,
            entity_type=e.type.value,
        )
        for e in list_entities(conn, limit=10000, field_id=field_id)
    ]
    if "resolve_entities" in client.skill_map():
        result, err = await call_skill_node(
            client,
            "resolve_entities",
            {
                "candidate_names": names,
                "existing_entities": [s.model_dump(mode="json") for s in existing],
            },
            traceparent=traceparent,
        )
        if result is not None:
            return [
                ResolvedEntityInfo.model_validate(r) for r in result.get("resolved", [])
            ]
        log.warning("resolve_entities_failed_falling_back", error=str(err))

    from mesh_agents.entity_tracker import resolve_entities_pure

    return resolve_entities_pure(names, existing)


def _persist_entities(
    conn: Any, resolved: list[ResolvedEntityInfo], *, field_id: str = DEFAULT_FIELD_ID
) -> tuple[dict[str, str], int]:
    """Persist is_new entities, return (name→entity_id map, created count)."""
    entity_map: dict[str, str] = {}
    created = 0
    for info in resolved:
        entity_map[info.name] = info.entity_id
        if info.is_new:
            try:
                create_entity(
                    conn,
                    Entity(
                        id=info.entity_id,
                        canonical_name=info.canonical_name,
                        type=EntityType(info.entity_type),
                    ),
                    field_id=field_id,
                )
                created += 1
            except Exception:
                pass  # already exists — concurrent insert / hash collision
    return entity_map, created


async def _resolve_and_persist(
    conn: Any,
    client: MeshA2AClient,
    embedder: Embedder | None,
    llm: LLMClient | None,
    names: list[str],
    traceparent: str,
    config: ResolutionConfig | None = None,
    *,
    field_id: str = DEFAULT_FIELD_ID,
) -> tuple[dict[str, str], int, list[ResolvedEntityInfo]]:
    """Resolve + persist candidate names, returning (name→id, created, infos).

    With an embedder (the live pipeline) this is the Phase 13 semantic path:
    alias fast-path → block → match, creating entities with embeddings. Without
    one (tests / no-embedder setups) it falls back to the exact-match A2A skill.
    All resolution + creation is scoped to ``field_id`` (Phase 17a).
    """
    resolved = await _resolve_entities(
        conn, client, names, traceparent, field_id=field_id
    )
    if embedder is None:
        # No embedder → exact-match only (tests / minimal setups).
        entity_map, created = _persist_entities(conn, resolved, field_id=field_id)
        return entity_map, created, resolved

    # Semantic path: keep the skill's exact-match + type assignment, but for
    # every name it would *create*, run a block→match guard first. On a semantic
    # match, attach to the canonical (with the skill-provided type as a hint);
    # otherwise create the new entity with its embedding. Existing exact matches
    # pass through untouched.
    emap: dict[str, str] = {}
    created = 0
    infos: list[ResolvedEntityInfo] = []
    for info in resolved:
        if info.is_new:
            final = resolve_entity_semantic(
                conn, embedder, llm, info.name,
                type_hint=info.entity_type, config=config, field_id=field_id,
            )
        else:
            final = info
        infos.append(final)
        emap[final.name] = final.entity_id
        if final.is_new:
            created += 1
    return emap, created, infos


def _insert_claims(
    conn: Any,
    pairs: list[tuple[ScoutedPaper, list[ExtractedClaim]]],
    entity_map: dict[str, str],
    url_to_investigation_id: dict[str, str],
    *,
    field_id: str = DEFAULT_FIELD_ID,
) -> tuple[list[ResolvedClaim], int]:
    """Insert claims for resolved subjects, attaching investigation lineage."""
    resolved_claims: list[ResolvedClaim] = []
    inserted = 0
    for paper, claims in pairs:
        for ec in claims:
            entity_id = entity_map.get(ec.subject_name)
            if entity_id is None:
                continue
            claim = Claim(
                predicate=ec.predicate,
                subject_entity_id=entity_id,
                object=ec.object,
                source_id=paper.source.id,
                extracted_by_agent="claim_extractor",
                raw_excerpt=ec.raw_excerpt,
                confidence=ec.confidence,
            )
            create_claim(conn, claim, field_id=field_id)
            inserted += 1
            inv_id = url_to_investigation_id.get(paper.source.url)
            if inv_id is not None:
                attach_claim_to_investigation(conn, inv_id, claim.id)
            resolved_claims.append(
                ResolvedClaim(
                    claim_id=claim.id,
                    subject_entity_id=entity_id,
                    predicate=ec.predicate,
                    object=ec.object,
                    source_id=paper.source.id,
                    raw_excerpt=ec.raw_excerpt,
                    confidence=ec.confidence,
                )
            )
    return resolved_claims, inserted


def _embed_belief(
    conn: Any, embedder: Embedder | None, belief_id: str, topic: str, statement: str
) -> None:
    """Populate a belief's ``statement_embedding`` from (topic, statement) so the
    Phase-19 consolidation sweep can block on it (Phase 19a).

    A local fastembed call — NOT an LLM — so the no-hot-path-LLM principle holds.
    No-ops when no embedder is wired (tests / minimal setups). Best-effort: an
    embedding failure must never abort synthesis (the backfill catches misses)."""
    if embedder is None:
        return
    try:
        vec = embedder.embed([belief_embed_text(topic, statement)])[0]
        set_belief_embedding(conn, belief_id, vec)
    except Exception:  # never let embedding bookkeeping abort a synthesis write
        log.warning("belief_embed_failed", belief_id=belief_id)


def _belief_confidence(
    conn: Any, belief_id: str, weights: ConfidenceWeights
) -> float:
    """Confidence from a belief's evidence signals (Phase 14d).

    Reads the belief_signals view (which already reflects the just-written claim
    links) and maps it to a confidence. Applies to every synthesized belief —
    score and capability alike — so confidence varies with real evidence instead
    of a hardcoded constant. Read-only; the caller writes it."""
    signals = BeliefSignals.from_row(get_belief_signals(conn, belief_id))
    return compute_confidence(signals, weights)


async def _run_sota(
    conn: Any,
    client: MeshA2AClient,
    resolved_claims: list[ResolvedClaim],
    traceparent: str,
    weights: ConfidenceWeights,
    embedder: Embedder | None = None,
    *,
    field_id: str = DEFAULT_FIELD_ID,
) -> tuple[int, int]:
    """Update SOTA beliefs from the resolved claims. Returns (created, revised)."""
    existing_sota = [
        BeliefSummary(
            belief_id=b.id, topic=b.topic, statement=b.statement, confidence=b.confidence
        )
        for b in list_beliefs(conn, currently_held=True, limit=1000, field_id=field_id)
        if b.topic.startswith("sota:")
    ]
    belief_updates: list[BeliefUpdate]
    if "update_sota" in client.skill_map():
        result, err = await call_skill_node(
            client,
            "update_sota",
            {
                "claims": [c.model_dump(mode="json") for c in resolved_claims],
                "existing_sota_beliefs": [b.model_dump(mode="json") for b in existing_sota],
            },
            traceparent=traceparent,
        )
        if result is not None:
            belief_updates = [
                BeliefUpdate.model_validate(u) for u in result.get("belief_updates", [])
            ]
        else:
            log.warning("update_sota_failed_falling_back", error=str(err))
            from mesh_agents.sota_tracker import update_sota_pure

            belief_updates = update_sota_pure(resolved_claims, existing_sota)
    else:
        from mesh_agents.sota_tracker import update_sota_pure

        belief_updates = update_sota_pure(resolved_claims, existing_sota)

    created = revised = 0
    for update in belief_updates:
        if update.is_new_belief:
            belief = Belief(
                topic=update.topic,
                statement=update.new_statement,
                supporting_claim_ids=update.supporting_claim_ids,
                confidence=update.new_confidence,
            )
            create_belief(conn, belief, field_id=field_id)
            # 14d: confidence comes from the belief's own evidence signals, not
            # the seed constant from the SOTA computation.
            update_belief(
                conn, belief.id, confidence=_belief_confidence(conn, belief.id, weights)
            )
            _embed_belief(conn, embedder, belief.id, belief.topic, belief.statement)
            created += 1
        else:
            assert update.existing_belief_id is not None
            existing_b = get_belief_by_id(conn, update.existing_belief_id)
            if existing_b is None:
                continue
            new_confidence = _belief_confidence(
                conn, update.existing_belief_id, weights
            )
            revision = BeliefRevision(
                belief_id=update.existing_belief_id,
                previous_statement=existing_b.statement,
                new_statement=update.new_statement,
                previous_confidence=existing_b.confidence,
                new_confidence=new_confidence,
                trigger_claim_ids=update.supporting_claim_ids,
                revised_by_agent="sota_tracker",
                rationale=update.rationale,
            )
            create_revision(conn, revision)
            update_belief(
                conn,
                update.existing_belief_id,
                statement=update.new_statement,
                confidence=new_confidence,
                last_revised_at=revision.revised_at,
                revision_count=existing_b.revision_count + 1,
            )
            # Statement changed → re-embed so blocking tracks the live statement.
            _embed_belief(
                conn, embedder, update.existing_belief_id,
                existing_b.topic, update.new_statement,
            )
            revised += 1
    return created, revised


def _run_capability(
    conn: Any,
    resolved_claims: list[ResolvedClaim],
    weights: ConfidenceWeights,
    embedder: Embedder | None = None,
    *,
    field_id: str = DEFAULT_FIELD_ID,
) -> tuple[int, int]:
    """Synthesize entity-anchored capability beliefs (Phase 14b). Returns
    (created, revised).

    For every entity touched by a capability claim this run, rebuild its
    ``capability:<entity_id>`` belief from the entity's *full* active capability
    claim set — so all capability claims about one resolved entity converge on a
    single belief and provenance stays complete. Coordinator-owned writes;
    idempotent (no change → no revision)."""
    cap_entity_ids = {
        rc.subject_entity_id
        for rc in resolved_claims
        if rc.claim_type == ClaimType.capability
    }
    if not cap_entity_ids:
        return 0, 0

    existing = {
        b.topic: b
        for b in list_beliefs(conn, currently_held=True, limit=1000, field_id=field_id)
        if b.topic.startswith(CAPABILITY_TOPIC_PREFIX)
    }

    created = revised = 0
    for entity_id in sorted(cap_entity_ids):
        ent = get_entity_by_id(conn, entity_id)
        name = ent.canonical_name if ent is not None else entity_id
        all_caps = list_claims(
            conn,
            entity_id=entity_id,
            claim_type=ClaimType.capability,
            status=ClaimStatus.active,
            limit=200,
            field_id=field_id,
        )
        claims = [
            ResolvedClaim(
                claim_id=c.id,
                subject_entity_id=c.subject_entity_id,
                predicate=c.predicate,
                claim_type=c.claim_type,
                object=c.object,
                source_id=c.source_id,
                raw_excerpt=c.raw_excerpt,
                confidence=c.confidence,
            )
            for c in all_caps
        ]
        existing_b = existing.get(capability_topic(entity_id))
        eb = (
            ExistingCapabilityBelief(
                belief_id=existing_b.id,
                statement=existing_b.statement,
                confidence=existing_b.confidence,
                supporting_claim_ids=existing_b.supporting_claim_ids,
            )
            if existing_b is not None
            else None
        )
        update = synthesize_capability_belief(
            CapabilityBeliefInput(
                entity_id=entity_id, entity_name=name, claims=claims, existing_belief=eb
            )
        )
        if update is None:
            continue
        if update.is_new_belief:
            belief = Belief(
                topic=update.topic,
                statement=update.new_statement,
                supporting_claim_ids=update.supporting_claim_ids,
                confidence=update.new_confidence,
            )
            create_belief(conn, belief, field_id=field_id)
            update_belief(
                conn, belief.id, confidence=_belief_confidence(conn, belief.id, weights)
            )
            _embed_belief(conn, embedder, belief.id, belief.topic, belief.statement)
            created += 1
        else:
            assert update.existing_belief_id is not None
            existing_full = get_belief_by_id(conn, update.existing_belief_id)
            if existing_full is None:
                continue
            revised_at = datetime.now(UTC)
            # Persist the new statement + claim links first so the signals (and
            # thus the recomputed confidence) reflect this run's evidence.
            update_belief(
                conn,
                update.existing_belief_id,
                statement=update.new_statement,
                supporting_claim_ids=update.supporting_claim_ids,
                last_revised_at=revised_at,
                revision_count=existing_full.revision_count + 1,
            )
            new_confidence = _belief_confidence(
                conn, update.existing_belief_id, weights
            )
            update_belief(conn, update.existing_belief_id, confidence=new_confidence)
            # Statement changed → re-embed so blocking tracks the live statement.
            _embed_belief(
                conn, embedder, update.existing_belief_id,
                existing_full.topic, update.new_statement,
            )
            create_revision(
                conn,
                BeliefRevision(
                    belief_id=update.existing_belief_id,
                    previous_statement=existing_full.statement,
                    new_statement=update.new_statement,
                    previous_confidence=existing_full.confidence,
                    new_confidence=new_confidence,
                    trigger_claim_ids=update.supporting_claim_ids,
                    revised_at=revised_at,
                    revised_by_agent="synthesizer",
                    rationale=update.rationale,
                ),
            )
            revised += 1
    return created, revised


def _edge_target_names(claims: list[ExtractedClaim]) -> set[str]:
    """Names of the *other* entity each relational claim points at, so the
    coordinator resolves them to nodes before synthesizing edges (Phase 14c)."""
    out: set[str] = set()
    for ec in claims:
        spec = edge_for_claim(claim_type_for_predicate(ec.predicate), ec.object)
        if spec is not None:
            out.add(spec[1])
    return out


def _run_edges(
    conn: Any,
    resolved_claims: list[ResolvedClaim],
    entity_map: dict[str, str],
    *,
    field_id: str = DEFAULT_FIELD_ID,
) -> int:
    """Synthesize claim-grounded relationship edges from relational claims
    (Phase 14c). Returns the number of *new* edges created (repeat assertions
    aggregate onto the existing edge instead). Coordinator-owned writes.

    Each edge connects the (Phase-13 canonical) subject entity to the resolved
    target entity named in the claim object; edges whose target didn't resolve,
    or that would self-loop, are skipped rather than fabricated."""
    created = 0
    for rc in resolved_claims:
        spec = edge_for_claim(rc.claim_type, rc.object)
        if spec is None:
            continue
        edge_type, target_name = spec
        target_id = entity_map.get(target_name)
        if target_id is None or target_id == rc.subject_entity_id:
            continue
        _rel, was_created = add_relationship_evidence(
            conn,
            rc.subject_entity_id,
            target_id,
            edge_type,
            rc.claim_id,
            rc.confidence,
            field_id=field_id,
        )
        if was_created:
            created += 1
    return created


async def _extract_papers(
    client: MeshA2AClient,
    papers: list[ScoutedPaper],
    traceparent: str,
    semaphore: asyncio.Semaphore,
    field_id: str = DEFAULT_FIELD_ID,
) -> tuple[
    list[tuple[ScoutedPaper, list[ExtractedClaim]]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Bounded-concurrency extraction used by the investigation node (the main
    path fans out through the graph instead).

    Returns (pairs, errors, usage_rows) where each usage row is
    ``{"usage": {...}, "model": "..."}`` for the coordinator to ledger."""

    async def _one(
        paper: ScoutedPaper,
    ) -> tuple[ScoutedPaper, list[ExtractedClaim], Any, dict[str, Any] | None]:
        async with semaphore:
            result, err = await call_skill_node(
                client,
                "extract_claims",
                {"paper": paper.model_dump(mode="json"), "field_id": field_id},
                traceparent=traceparent,
                context={"arxiv_id": paper.arxiv_id},
            )
        if result is not None:
            claims = [ExtractedClaim.model_validate(c) for c in result.get("claims", [])]
            usage_row = {"usage": result.get("usage") or {}, "model": result.get("model") or ""}
            return paper, claims, None, usage_row
        return paper, [], err, None

    pairs: list[tuple[ScoutedPaper, list[ExtractedClaim]]] = []
    errors: list[dict[str, Any]] = []
    usage_rows: list[dict[str, Any]] = []
    for paper, claims, err, usage_row in await asyncio.gather(*(_one(p) for p in papers)):
        if err is not None:
            errors.append(err.model_dump())
        else:
            pairs.append((paper, claims))
        if usage_row is not None:
            usage_rows.append(usage_row)
    return pairs, errors, usage_rows


def _open_investigations(conn: Any, *, field_id: str = DEFAULT_FIELD_ID) -> list[Any]:
    return list_investigations(
        conn, status=InvestigationStatus.open, limit=100, field_id=field_id
    ) + list_investigations(
        conn, status=InvestigationStatus.in_progress, limit=100, field_id=field_id
    )


def _investigation_lifecycle(conn: Any, investigations: list[Any]) -> tuple[int, int]:
    """Resolve investigations that cleared the claim threshold; abandon those
    that exhausted their run budget. Returns (resolved, abandoned)."""
    threshold = _investigation_claims_threshold()
    max_runs = _investigation_max_runs()
    resolved = abandoned = 0
    for inv in investigations:
        current = update_investigation(conn, inv.id)  # re-fetch
        if len(current.collected_claim_ids) >= threshold:
            update_investigation(
                conn,
                inv.id,
                status=InvestigationStatus.resolved,
                resolved_at=datetime.now(UTC),
            )
            resolved += 1
        elif current.pipeline_runs_attempted >= max_runs:
            update_investigation(
                conn,
                inv.id,
                status=InvestigationStatus.abandoned,
                resolved_at=datetime.now(UTC),
            )
            abandoned += 1
    return resolved, abandoned


def _paper_id_for_error(err: dict[str, Any]) -> str:
    ctx = err.get("context") or {}
    return str(ctx.get("arxiv_id") or ctx.get("investigation_id") or err.get("skill_id") or "")


def _item_identity(paper: ScoutedPaper) -> tuple[str, str, str]:
    """(source_type, external_id, content_hash) used to key the dedup ledger.

    external_id is the source URL — stable and present for every scout (arxiv
    versions carry the version in the URL, so a new version is a new item)."""
    return (
        paper.source.type.value,
        paper.source.url,
        paper.source.raw_content_hash,
    )


def _dedup_for_extraction(
    conn: Any, papers: list[ScoutedPaper], now: datetime, *, field_id: str = DEFAULT_FIELD_ID
) -> tuple[list[ScoutedPaper], int]:
    """Partition scouted papers into (to_extract, skipped_count) using the
    processed_items ledger. Unseen + content-changed items are extracted;
    unchanged items are skipped (their last_seen_at bumped). Intra-batch
    duplicates by (source_type, external_id) collapse to the first occurrence."""
    to_extract: list[ScoutedPaper] = []
    skipped = 0
    seen_in_batch: set[tuple[str, str]] = set()
    for paper in papers:
        source_type, external_id, content_hash = _item_identity(paper)
        key = (source_type, external_id)
        if key in seen_in_batch:
            continue
        seen_in_batch.add(key)
        decision = decide(
            conn, source_type, external_id, content_hash, field_id=field_id
        )
        if decision is ProcessedDecision.unchanged:
            touch_processed_item(conn, source_type, external_id, now, field_id=field_id)
            skipped += 1
        else:
            to_extract.append(paper)
    return to_extract, skipped


def _persist_llm_usage(
    conn: Any,
    run_id: str,
    skill_id: str,
    agent_name: str,
    usage_dict: dict[str, Any] | None,
    model: str,
) -> None:
    """Write one llm_usage ledger row (coordinator is the single writer).

    No-ops when the call recorded no tokens (e.g. a parse failure that never
    reached the provider)."""
    usage = LLMUsage(**(usage_dict or {}))
    if usage.total_tokens == 0:
        return
    cost = estimate_cost(model, usage)
    create_llm_usage(
        conn,
        LLMUsageRecord(
            run_id=run_id,
            agent_name=agent_name,
            skill_id=skill_id,
            model=model or None,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            estimated_cost_usd=cost.total_cost,
        ),
    )


def _avg_latency(extractions: list[dict[str, Any]]) -> int:
    latencies = [int(e["latency_ms"]) for e in extractions if e.get("latency_ms")]
    return int(sum(latencies) / len(latencies)) if latencies else 0


# ── shared investigation dispatch ─────────────────────────────────────────────


async def dispatch_open_investigations(
    *,
    client: MeshA2AClient,
    conn: Any,
    embedder: Embedder | None,
    llm: LLMClient | None,
    semaphore: asyncio.Semaphore,
    resolution_config: ResolutionConfig,
    field_id: str,
    traceparent: str,
    run_id: str,
    investigations: list[Any],
    max_fetch: int | None = None,
) -> dict[str, Any]:
    """Run hypothesis-directed search for a set of open investigations, then feed
    the gathered sources through extract → resolve → insert-claims — the exact
    path the pipeline's ``dispatch_investigations`` node uses. Factored out
    (Phase 22d) so the proactive discovery sweep reuses it instead of forking the
    Investigation plumbing. ``max_fetch`` optionally caps total source records
    gathered this call (``MESH_DISCOVER_MAX_FETCH``); ``None`` means uncapped.

    Field-scoped: only dispatches to investigate sources backed by a connector
    ENABLED for ``field_id``. Fetch failures record into the returned ``errors``
    and never raise — one bad search never aborts the run."""
    skill_map = client.skill_map()
    enabled_sources = {
        investigate_source_name(fc.connector_id)
        for fc in list_field_connectors(conn, field_id, enabled_only=True)
    }
    errors: list[dict[str, Any]] = []
    investigation_papers: dict[str, list[str]] = {}
    gathered: list[ScoutedPaper] = []
    dispatched = 0

    for inv in investigations:
        if max_fetch is not None and dispatched >= max_fetch:
            break
        update_investigation(
            conn,
            inv.id,
            status=InvestigationStatus.in_progress,
            pipeline_runs_attempted=inv.pipeline_runs_attempted + 1,
        )
        for source_type in inv.suggested_source_types:
            if max_fetch is not None and dispatched >= max_fetch:
                break
            if source_type not in enabled_sources:
                continue  # not enabled for this field
            skill_id = f"investigate_{source_type}"
            if skill_id not in skill_map:
                continue  # connector advertises no investigate handler
            result, err = await call_skill_node(
                client,
                skill_id,
                {
                    "investigation_id": inv.id,
                    "hypothesis": inv.hypothesis or inv.question,
                    "target_entity_id": inv.target_entity_id,
                    "suggested_source_types": inv.suggested_source_types,
                    "max_results": 10,
                },
                traceparent=traceparent,
                context={"investigation_id": inv.id},
            )
            if err is not None or result is None:
                if err is not None:
                    errors.append(err.model_dump())
                continue
            for raw in result.get("source_records", []):
                paper = ScoutedPaper.model_validate(raw)
                gathered.append(paper)
                investigation_papers.setdefault(inv.id, []).append(paper.source.url)
                dispatched += 1

    new_papers = _dedup_and_insert_sources(conn, gathered, field_id=field_id)
    pairs, extract_errors, usage_rows = await _extract_papers(
        client, new_papers, traceparent, semaphore, field_id=field_id
    )
    errors.extend(extract_errors)
    for row in usage_rows:
        _persist_llm_usage(
            conn,
            run_id,
            "extract_claims",
            "claim_extractor",
            row.get("usage"),
            str(row.get("model") or ""),
        )

    names = {ec.subject_name for _, claims in pairs for ec in claims}
    entity_map, created, _resolved = await _resolve_and_persist(
        conn, client, embedder, llm, list(names), traceparent, resolution_config,
        field_id=field_id,
    )
    url_to_inv = {
        url: inv_id for inv_id, urls in investigation_papers.items() for url in urls
    }
    _, inserted = _insert_claims(conn, pairs, entity_map, url_to_inv, field_id=field_id)
    resolved_count, abandoned_count = _investigation_lifecycle(conn, investigations)
    log.info(
        "investigations_dispatched",
        dispatched=dispatched,
        claims=inserted,
        resolved=resolved_count,
        abandoned=abandoned_count,
    )
    return {
        "investigations_dispatched": dispatched,
        "investigation_claims_inserted": inserted,
        "investigation_entities_created": created,
        "investigations_resolved": resolved_count,
        "investigations_abandoned": abandoned_count,
        "errors": errors,
    }


# ── graph construction ───────────────────────────────────────────────────────


def build_coordinator_graph(
    client: MeshA2AClient,
    conn: Any,
    semaphore: asyncio.Semaphore,
    embedder: Embedder | None = None,
    llm: LLMClient | None = None,
) -> StateGraph[CoordinatorState, Any, Any, Any]:
    """Build the coordinator graph. Nodes close over the live A2A client +
    Postgres connection (non-serializable, so kept out of checkpointed state).

    ``embedder`` (+ optional ``llm`` for adjudication) enables Phase 13 semantic
    entity resolution; when absent, resolution falls back to exact-match."""
    resolution_config = ResolutionConfig.from_env()
    confidence_weights = ConfidenceWeights.from_env()

    async def scout(state: CoordinatorState) -> dict[str, Any]:
        discovered = await client.discover(_agent_urls())
        discovered_scouts = {sid for sid in discovered if sid.startswith("scout_")}
        # Dispatch only the connectors ENABLED for this run's field, each with its
        # stored per-field config (17c). _agent_urls() are merely the *available*
        # connector services; which run is field-driven.
        enabled = list_field_connectors(conn, state["field_id"], enabled_only=True)
        configs: dict[str, dict[str, Any]] = {}
        for fc in enabled:
            skill = connector_skill_id(fc.connector_id)
            if skill in discovered_scouts:
                configs[skill] = fc.config
        if not configs:
            raise SystemExit(
                "No enabled connectors discovered for field "
                f"{state['field_id']} — aborting pipeline"
            )
        scout_ids = sorted(configs)
        log.info("scouts_discovered", scout_skills=scout_ids, field_id=state["field_id"])
        return {"scout_skill_ids": scout_ids, "scout_configs": configs}

    def fan_scouts(state: CoordinatorState) -> list[Send]:
        tp = state["traceparent"]
        sends: list[Send] = []
        for sid in state["scout_skill_ids"]:
            config = state["scout_configs"].get(sid, {})
            payload: dict[str, Any] = {**config, "max_results": state["max_papers"]}
            if state["since"] is not None:
                payload["since"] = state["since"]
            # Legacy --categories override applies to the connector that takes
            # categories (arxiv).
            if state["categories"] is not None and "categories" in config:
                payload["categories"] = state["categories"]
            sends.append(
                Send(
                    "scout_one",
                    {"skill_id": sid, "payload": payload, "traceparent": tp},
                )
            )
        return sends

    async def scout_one(state: _ScoutWork) -> dict[str, Any]:
        result, err = await call_skill_node(
            client,
            state["skill_id"],
            state["payload"],
            traceparent=state["traceparent"],
        )
        patch: dict[str, Any] = {}
        if result is not None:
            patch["raw_papers"] = list(result.get("papers", []))
        if err is not None:
            patch["errors"] = [err.model_dump()]
        return patch

    async def ingest(state: CoordinatorState) -> dict[str, Any]:
        field_id = state["field_id"]
        papers = [ScoutedPaper.model_validate(p) for p in state["raw_papers"]]
        now = datetime.now(UTC)
        # Dedup gate: skip extraction for items already processed (unchanged
        # content). Only unseen / content-changed items proceed to extraction.
        to_extract, skipped = _dedup_for_extraction(conn, papers, now, field_id=field_id)
        new_papers = _dedup_and_insert_sources(conn, to_extract, field_id=field_id)
        log.info(
            "ingest",
            items_seen=len(papers),
            items_skipped=skipped,
            items_to_extract=len(to_extract),
            new_sources=len(new_papers),
        )
        return {
            "new_papers": [p.model_dump(mode="json") for p in new_papers],
            "papers_scouted": len(papers),
            "sources_inserted": len(new_papers),
            "items_skipped": skipped,
        }

    def route_after_ingest(state: CoordinatorState) -> list[Send] | str:
        if not state["new_papers"] or "extract_claims" not in client.skill_map():
            return "finalize"
        tp = state["traceparent"]
        fid = state["field_id"]
        return [
            Send("extract_one", {"paper": p, "traceparent": tp, "field_id": fid})
            for p in state["new_papers"]
        ]

    async def extract_one(state: _ExtractWork) -> dict[str, Any]:
        paper = state["paper"]
        arxiv_id = paper.get("arxiv_id") or (paper.get("source") or {}).get("url", "")
        async with semaphore:
            result, err = await call_skill_node(
                client,
                "extract_claims",
                {"paper": paper, "field_id": state["field_id"]},
                traceparent=state["traceparent"],
                context={"arxiv_id": arxiv_id},
            )
        patch: dict[str, Any] = {}
        if result is not None:
            patch["extractions"] = [
                {
                    "paper": paper,
                    "claims": list(result.get("claims", [])),
                    "latency_ms": int(result.get("latency_ms") or 0),
                    "usage": result.get("usage") or {},
                    "model": result.get("model") or "",
                }
            ]
        if err is not None:
            patch["errors"] = [err.model_dump()]
        return patch

    def route_after_extract(state: CoordinatorState) -> str:
        total = sum(len(e["claims"]) for e in state["extractions"])
        log.info("claims_extracted_total", total=total)
        return "track_entities" if total > 0 else "finalize"

    async def track_entities(state: CoordinatorState) -> dict[str, Any]:
        pairs: list[tuple[ScoutedPaper, list[ExtractedClaim]]] = []
        names: set[str] = set()
        for e in state["extractions"]:
            paper = ScoutedPaper.model_validate(e["paper"])
            claims = [ExtractedClaim.model_validate(c) for c in e["claims"]]
            pairs.append((paper, claims))
            names.update(ec.subject_name for ec in claims)
            # Relational claims also reference a *target* entity (in the object);
            # resolve those too so 14c can ground edges on real nodes.
            names.update(_edge_target_names(claims))
        field_id = state["field_id"]
        entity_map, created, resolved = await _resolve_and_persist(
            conn, client, embedder, llm, list(names),
            state["traceparent"], resolution_config, field_id=field_id,
        )
        resolved_claims, inserted = _insert_claims(
            conn, pairs, entity_map, {}, field_id=field_id
        )
        relationships_created = _run_edges(
            conn, resolved_claims, entity_map, field_id=field_id
        )
        has_mb = any(r.entity_type in _MODEL_LIKE_TYPES for r in resolved)
        log.info(
            "entities_resolved",
            total=len(entity_map),
            created=created,
            claims=inserted,
            edges=relationships_created,
        )
        return {
            "entities_created": created,
            "claims_inserted": inserted,
            "resolved_claims": [rc.model_dump(mode="json") for rc in resolved_claims],
            "has_model_or_benchmark_entity": has_mb,
            "relationships_created": relationships_created,
        }

    def route_after_entities(state: CoordinatorState) -> str:
        # Synthesize whenever the run produced any resolved claims — the score
        # handler self-filters to leaderboard claims, and the capability handler
        # attaches to any entity type, so the old model/benchmark gate is too
        # narrow now.
        return "synthesize" if state["resolved_claims"] else "curate"

    async def synthesize(state: CoordinatorState) -> dict[str, Any]:
        # Type-routed synthesis (Phase 14b): the leaderboard path is now one
        # handler among several. `score` claims feed the (unchanged) SOTA
        # handler; `capability` claims feed entity-anchored belief synthesis.
        resolved_claims = [
            ResolvedClaim.model_validate(rc) for rc in state["resolved_claims"]
        ]
        field_id = state["field_id"]
        sota_created, sota_revised = await _run_sota(
            conn, client, resolved_claims, state["traceparent"], confidence_weights,
            embedder, field_id=field_id,
        )
        cap_created, cap_revised = _run_capability(
            conn, resolved_claims, confidence_weights, embedder, field_id=field_id
        )
        log.info(
            "beliefs_synthesized",
            sota_created=sota_created,
            sota_revised=sota_revised,
            capability_created=cap_created,
            capability_revised=cap_revised,
        )
        return {
            "beliefs_created": sota_created + cap_created,
            "beliefs_revised": sota_revised + cap_revised,
        }

    async def curate(state: CoordinatorState) -> dict[str, Any]:
        # The Curator is a sweep-time agent (skill: select_beliefs_to_challenge)
        # and is not part of the coordinator's discovered topology in any
        # deployment, so there's no claim-set curation skill to call here. This
        # node's job is to decide whether the run has open investigations to
        # dispatch; it's also the documented hook where a coordinator-side
        # curator dispatch would live if one is ever wired in.
        has_open = len(_open_investigations(conn, field_id=state["field_id"])) > 0
        return {"has_open_investigations": has_open}

    def route_after_curate(state: CoordinatorState) -> str:
        return "dispatch_investigations" if state["has_open_investigations"] else "finalize"

    async def dispatch_investigations(state: CoordinatorState) -> dict[str, Any]:
        field_id = state["field_id"]
        investigations = _open_investigations(conn, field_id=field_id)
        if not investigations:
            return {}
        return await dispatch_open_investigations(
            client=client,
            conn=conn,
            embedder=embedder,
            llm=llm,
            semaphore=semaphore,
            resolution_config=resolution_config,
            field_id=field_id,
            traceparent=state["traceparent"],
            run_id=state["run_id"],
            investigations=investigations,
        )

    async def finalize(state: CoordinatorState) -> dict[str, Any]:
        avg = _avg_latency(state["extractions"])
        # Idempotency guard: a checkpointed graph can re-tick the final
        # superstep. If this run's row already exists, finalize already ran —
        # skip re-writing the run row and the llm_usage ledger.
        if pipeline_run_exists(conn, state["run_id"]):
            log.info("finalize_already_done", run_id=state["run_id"])
            return {"finalized": True, "avg_extraction_latency_ms": avg}
        # Persist per-call token usage for every extraction (main fan-out path).
        # Investigation re-extraction usage is recorded in dispatch_investigations.
        # Record each extracted item in the dedup ledger now that it has been
        # processed (record-after-extract, so a failed extraction retries next run).
        now = datetime.now(UTC)
        for e in state["extractions"]:
            _persist_llm_usage(
                conn,
                state["run_id"],
                "extract_claims",
                "claim_extractor",
                e.get("usage"),
                str(e.get("model") or ""),
            )
            try:
                paper = ScoutedPaper.model_validate(e["paper"])
                source_type, external_id, content_hash = _item_identity(paper)
                record_processed_item(
                    conn, source_type, external_id, content_hash, now,
                    field_id=state["field_id"],
                )
            except Exception:  # never let ledger bookkeeping abort finalize
                log.warning("processed_item_record_failed", paper=e.get("paper"))
        errors = [
            PipelineError(
                paper_id=_paper_id_for_error(e),
                error_type=str(e["error_type"]),
                error_message=str(e["error_message"]),
            )
            for e in state["errors"]
        ]
        run = PipelineRun(
            id=state["run_id"],
            started_at=datetime.fromisoformat(state["started_at"]),
            finished_at=datetime.now(UTC),
            triggered_by=state["triggered_by"],
            papers_scouted=state["papers_scouted"],
            sources_inserted=state["sources_inserted"],
            claims_inserted=state["claims_inserted"] + state["investigation_claims_inserted"],
            entities_created=state["entities_created"] + state["investigation_entities_created"],
            beliefs_created=state["beliefs_created"],
            beliefs_revised=state["beliefs_revised"],
            avg_extraction_latency_ms=avg,
            errors=errors,
        )
        create_pipeline_run(conn, run, field_id=state["field_id"])
        log.info(
            "coordinator_complete",
            run_id=run.id,
            sources_inserted=run.sources_inserted,
            claims_inserted=run.claims_inserted,
            entities_created=run.entities_created,
            beliefs_created=run.beliefs_created,
            beliefs_revised=run.beliefs_revised,
            errors=len(errors),
        )
        return {"finalized": True, "avg_extraction_latency_ms": avg}

    g: StateGraph[CoordinatorState, Any, Any, Any] = StateGraph(CoordinatorState)
    g.add_node("scout", scout)
    # Fan-out workers read a per-item Send payload, not the graph state, so
    # they declare their own input schema.
    g.add_node("scout_one", scout_one, input_schema=_ScoutWork)
    g.add_node("ingest", ingest)
    g.add_node("extract_one", extract_one, input_schema=_ExtractWork)
    g.add_node("track_entities", track_entities)
    g.add_node("synthesize", synthesize)
    g.add_node("curate", curate)
    g.add_node("dispatch_investigations", dispatch_investigations)
    g.add_node("finalize", finalize)

    g.add_edge(START, "scout")
    g.add_conditional_edges("scout", fan_scouts, ["scout_one"])
    g.add_edge("scout_one", "ingest")
    g.add_conditional_edges("ingest", route_after_ingest, ["extract_one", "finalize"])
    g.add_conditional_edges("extract_one", route_after_extract, ["track_entities", "finalize"])
    g.add_conditional_edges("track_entities", route_after_entities, ["synthesize", "curate"])
    g.add_edge("synthesize", "curate")
    g.add_conditional_edges("curate", route_after_curate, ["dispatch_investigations", "finalize"])
    g.add_edge("dispatch_investigations", "finalize")
    g.add_edge("finalize", END)
    return g


def _make_resolution_deps() -> tuple[Embedder | None, LLMClient | None]:
    """Build the entity-resolution embedder + (best-effort) adjudication LLM for
    the live pipeline. A missing/unready LLM degrades to high-band/exact-only
    resolution (no middle-band adjudication) rather than aborting the run."""
    from mesh_llm import make_embedder, make_routed_llm_client

    embedder = make_embedder()
    llm: LLMClient | None
    try:
        llm = make_routed_llm_client(agent_name="entity_resolution")
        llm.health_check()
    except Exception as exc:
        log.warning("entity_resolution_llm_unavailable", error=str(exc))
        llm = None
    return embedder, llm


def _result_from_state(state: CoordinatorState) -> PipelineResult:
    errors = [
        {
            "paper_id": _paper_id_for_error(e),
            "error_type": str(e["error_type"]),
            "error_message": str(e["error_message"]),
        }
        for e in state["errors"]
    ]
    return PipelineResult(
        run_id=state["run_id"],
        papers_scouted=state["papers_scouted"],
        sources_inserted=state["sources_inserted"],
        items_skipped=state.get("items_skipped", 0),
        claims_inserted=state["claims_inserted"] + state["investigation_claims_inserted"],
        entities_created=state["entities_created"] + state["investigation_entities_created"],
        beliefs_created=state["beliefs_created"],
        beliefs_revised=state["beliefs_revised"],
        relationships_created=state.get("relationships_created", 0),
        avg_extraction_latency_ms=state.get("avg_extraction_latency_ms", 0),
        errors=errors,
    )


async def run_pipeline(
    categories: list[str] | None,
    max_papers: int,
    since: datetime | None,
    db_path: str | None = None,
    field: str = DEFAULT_FIELD_SLUG,
) -> PipelineResult:
    """Phase 8 coordinator pipeline, driven by a LangGraph graph.

    Discovers agents via A2A cards and dispatches by skill id, exactly as
    before; orchestration state is checkpointed (Postgres in docker,
    in-memory locally) with thread_id == run_id.

    ``field`` (slug) scopes every read/write of field-state to one field
    (default ``ai-robotics`` — the seeded field that reproduces prior behavior).
    """
    log.info(
        "coordinator_starting", categories=categories, max_papers=max_papers, field=field
    )

    conn = get_connection(db_path)
    init_pg()

    # Resolve the field slug → id (the seeded ai-robotics field's id == slug).
    field_row = get_field_by_slug(conn, field)
    field_id = field_row.id if field_row is not None else DEFAULT_FIELD_ID

    # A manual trigger from the API/scheduler can pin the run id (so the
    # returned id matches this run's pipeline_runs row + checkpoint thread).
    run_id = os.environ.get("MESH_RUN_ID") or str(uuid.uuid4())
    initial_state: CoordinatorState = {
        "run_id": run_id,
        "field_id": field_id,
        "triggered_by": os.environ.get("MESH_TRIGGERED_BY", "manual"),
        "traceparent": new_traceparent(),
        "started_at": datetime.now(UTC).isoformat(),
        "categories": categories,
        "max_papers": max_papers,
        "since": since.isoformat() if since is not None else None,
        "scout_skill_ids": [],
        "scout_configs": {},
        "raw_papers": [],
        "new_papers": [],
        "papers_scouted": 0,
        "sources_inserted": 0,
        "items_skipped": 0,
        "extractions": [],
        "avg_extraction_latency_ms": 0,
        "entities_created": 0,
        "claims_inserted": 0,
        "resolved_claims": [],
        "has_model_or_benchmark_entity": False,
        "beliefs_created": 0,
        "beliefs_revised": 0,
        "relationships_created": 0,
        "has_open_investigations": False,
        "investigations_dispatched": 0,
        "investigations_resolved": 0,
        "investigations_abandoned": 0,
        "investigation_claims_inserted": 0,
        "investigation_entities_created": 0,
        "errors": [],
        "finalized": False,
    }

    semaphore = asyncio.Semaphore(_get_concurrency())
    embedder, llm = _make_resolution_deps()
    async with MeshA2AClient() as client:
        graph = build_coordinator_graph(client, conn, semaphore, embedder, llm)
        async with open_checkpointer() as saver:
            app = graph.compile(checkpointer=saver)
            final = await app.ainvoke(initial_state, config=thread_config(run_id))
    conn.close()
    return _result_from_state(cast(CoordinatorState, final))


def parse_since(since_str: str | None) -> datetime | None:
    if since_str is None:
        return None
    if since_str.endswith("h"):
        return datetime.now(UTC) - timedelta(hours=int(since_str[:-1]))
    if since_str.endswith("d"):
        return datetime.now(UTC) - timedelta(days=int(since_str[:-1]))
    return datetime.fromisoformat(since_str)
