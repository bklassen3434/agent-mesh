"""A2A coordinator — Phase 2 replacement for the in-process orchestrator.

Dispatches to agent servers via skill-based capability discovery.
The coordinator owns all DB reads and writes; agents are pure functions.
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from mesh_a2a.client import MeshA2AClient, SkillCallError, SkillNotFoundError
from mesh_a2a.tracing import new_traceparent
from mesh_agents.arxiv_scout import ScoutedPaper
from mesh_agents.claim_extractor import ExtractedClaim
from mesh_agents.entity_tracker import EntitySummary, ResolvedEntityInfo
from mesh_agents.sota_tracker import BeliefSummary, BeliefUpdate, ResolvedClaim
from mesh_db.agent_tasks import DuckDBTaskRecorder, sweep_orphaned_tasks
from mesh_db.beliefs import create_belief, get_belief_by_id, list_beliefs, update_belief
from mesh_db.claims import create_claim
from mesh_db.connection import get_connection
from mesh_db.entities import list_entities
from mesh_db.investigations import (
    attach_claim_to_investigation,
    list_investigations,
    update_investigation,
)
from mesh_db.migrations import apply_migrations
from mesh_db.pipeline_runs import PipelineError, PipelineRun, create_pipeline_run
from mesh_db.revisions import create_revision
from mesh_db.sources import create_source, list_sources
from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.entity import Entity, EntityType
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


class PipelineResult(BaseModel):
    run_id: str
    papers_scouted: int
    sources_inserted: int
    claims_inserted: int
    entities_created: int
    beliefs_created: int
    beliefs_revised: int
    avg_extraction_latency_ms: int
    errors: list[dict[str, str]]


def _get_concurrency() -> int:
    return int(os.environ.get("MESH_PIPELINE_CONCURRENCY", "3"))


def _agent_urls() -> list[str]:
    raw = os.environ.get("MESH_AGENT_URLS", "")
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]
    return _DEFAULT_AGENT_URLS


def _task_resume_threshold() -> int:
    """Seconds an agent_task can sit pending/running before the startup
    sweep considers it orphaned. Default 600s = 10 minutes."""
    return int(os.environ.get("MESH_TASK_RESUME_THRESHOLD", "600"))


def _investigation_claims_threshold() -> int:
    """Collected claims needed to mark an Investigation resolved."""
    return int(os.environ.get("MESH_INVESTIGATION_CLAIMS_THRESHOLD", "3"))


def _investigation_max_runs() -> int:
    """Pipeline runs to attempt before abandoning an Investigation that
    keeps coming up empty."""
    return int(os.environ.get("MESH_INVESTIGATION_MAX_RUNS", "5"))


async def run_pipeline(
    categories: list[str],
    max_papers: int,
    since: datetime | None,
    db_path: str | None = None,
) -> PipelineResult:
    """Phase 2 coordinator pipeline.

    Discovers agents via A2A cards, dispatches by skill ID.
    Coordinator owns all DB writes; no agent class is imported for dispatch.
    """
    log.info("coordinator_starting", categories=categories, max_papers=max_papers)

    conn = get_connection(db_path)
    apply_migrations(conn)

    run = PipelineRun(
        started_at=datetime.now(UTC),
        triggered_by=os.environ.get("MESH_TRIGGERED_BY", "manual"),
    )
    errors: list[PipelineError] = []
    latencies: list[int] = []
    traceparent = new_traceparent()

    # Phase 6b: persist every skill dispatch through a DuckDB-backed
    # recorder so the status page + post-crash debugging have something
    # to look at. Orphaned tasks from a previous run get marked failed
    # before we add fresh ones, so the table doesn't grow unbounded with
    # stale rows.
    sweep_orphaned_tasks(conn, threshold_seconds=_task_resume_threshold())
    recorder = DuckDBTaskRecorder(conn, dispatched_by_run_id=run.id)

    async with MeshA2AClient(task_recorder=recorder) as client:
        # ── 0. Discover agents ─────────────────────────────────────────────
        discovered = await client.discover(_agent_urls())
        log.info("agents_discovered", skills=list(discovered.keys()))

        # ── 1. Scout all sources via skill_id discovery ────────────────────
        # Any agent that advertises a "scout_*" skill is dispatched. New
        # source types (HN, GitHub, …) drop in by publishing their card; the
        # coordinator never grows a per-scout branch.
        scout_ids = sorted(sid for sid in discovered if sid.startswith("scout_"))
        if not scout_ids:
            raise SystemExit("No scout agents discovered — aborting pipeline")
        log.info("scouts_discovered", scout_skills=scout_ids)

        scout_payload: dict[str, Any] = {
            "categories": categories,
            "max_results": max_papers,
        }
        if since is not None:
            scout_payload["since"] = since.isoformat()

        papers: list[ScoutedPaper] = []
        for scout_id in scout_ids:
            scout_result = await client.call_skill_blocking(
                scout_id, scout_payload, traceparent=traceparent
            )
            for p in scout_result.get("papers", []):
                papers.append(ScoutedPaper.model_validate(p))
            log.info("scout_returned", scout=scout_id, total_papers_so_far=len(papers))

        # ── 1b. Investigation dispatch (Phase 7a) ──────────────────────────
        # Each open investigation gets dispatched to the scouts matching its
        # suggested_source_types. Results are appended to `papers` and tagged
        # via investigation_papers so claim attachment can wire them once
        # extraction finishes.
        investigation_papers: dict[str, list[str]] = {}  # inv_id -> source.url list
        open_investigations = list_investigations(
            conn, status=InvestigationStatus.open, limit=100
        ) + list_investigations(
            conn, status=InvestigationStatus.in_progress, limit=100
        )
        for inv in open_investigations:
            # Bump attempt counter + transition to in_progress.
            update_investigation(
                conn,
                inv.id,
                status=InvestigationStatus.in_progress,
                pipeline_runs_attempted=inv.pipeline_runs_attempted + 1,
            )
            n_new_for_inv = 0
            for source_type in inv.suggested_source_types:
                skill_id = f"investigate_{source_type}"
                if skill_id not in discovered:
                    continue
                try:
                    inv_result = await client.call_skill_blocking(
                        skill_id,
                        {
                            "investigation_id": inv.id,
                            "hypothesis": inv.hypothesis or inv.question,
                            "target_entity_id": inv.target_entity_id,
                            "suggested_source_types": inv.suggested_source_types,
                            "max_results": 10,
                        },
                        traceparent=traceparent,
                    )
                except (SkillNotFoundError, SkillCallError) as exc:
                    log.warning(
                        "investigate_dispatch_failed",
                        skill=skill_id,
                        investigation_id=inv.id,
                        error=str(exc),
                    )
                    continue
                for raw in inv_result.get("source_records", []):
                    paper = ScoutedPaper.model_validate(raw)
                    papers.append(paper)
                    investigation_papers.setdefault(inv.id, []).append(paper.source.url)
                    n_new_for_inv += 1
            log.info(
                "investigation_dispatched",
                investigation_id=inv.id,
                source_records_returned=n_new_for_inv,
            )

        run.papers_scouted = len(papers)

        # ── 2. Deduplicate ─────────────────────────────────────────────────
        existing_hashes: set[str] = {
            s.raw_content_hash for s in list_sources(conn, limit=10000)
        }
        new_papers = [p for p in papers if p.source.raw_content_hash not in existing_hashes]
        log.info("new_papers", count=len(new_papers), skipped=len(papers) - len(new_papers))

        # ── 3. Insert new sources ──────────────────────────────────────────
        for paper in new_papers:
            create_source(conn, paper.source)
        run.sources_inserted = len(new_papers)

        if not new_papers:
            run.finished_at = datetime.now(UTC)
            create_pipeline_run(conn, run)
            conn.close()
            return _to_result(run)

        # ── 4. Extract claims (bounded concurrency) ────────────────────────
        if "extract_claims" not in discovered:
            log.warning("claim_extractor_not_discovered_skipping")
            run.finished_at = datetime.now(UTC)
            create_pipeline_run(conn, run)
            conn.close()
            return _to_result(run)

        semaphore = asyncio.Semaphore(_get_concurrency())
        extraction_results: list[tuple[ScoutedPaper, list[ExtractedClaim], int]] = []

        async def extract_one(paper: ScoutedPaper) -> None:
            async with semaphore:
                try:
                    result = await client.call_skill_blocking(
                        "extract_claims",
                        {"paper": paper.model_dump(mode="json")},
                        traceparent=traceparent,
                    )
                    claims_raw = result.get("claims", [])
                    claims = [ExtractedClaim.model_validate(c) for c in claims_raw]
                    latency_ms = int(result.get("latency_ms") or 0)
                    extraction_results.append((paper, claims, latency_ms))
                    log.info("claims_extracted", arxiv_id=paper.arxiv_id, count=len(claims))
                    if latency_ms > 0:
                        latencies.append(latency_ms)
                except (SkillNotFoundError, SkillCallError) as exc:
                    errors.append(
                        PipelineError(
                            paper_id=paper.arxiv_id,
                            error_type=type(exc).__name__,
                            error_message=str(exc),
                        )
                    )
                    log.warning("extraction_failed", arxiv_id=paper.arxiv_id, error=str(exc))
                except Exception as exc:
                    errors.append(
                        PipelineError(
                            paper_id=paper.arxiv_id,
                            error_type=type(exc).__name__,
                            error_message=str(exc),
                        )
                    )
                    log.warning("extraction_error", arxiv_id=paper.arxiv_id, error=str(exc))

        await asyncio.gather(*(extract_one(p) for p in new_papers))

        # ── 5. Collect candidate entity names ─────────────────────────────
        all_names: set[str] = set()
        for _, claims, _ in extraction_results:
            for ec in claims:
                all_names.add(ec.subject_name)

        if not all_names:
            run.finished_at = datetime.now(UTC)
            create_pipeline_run(conn, run)
            conn.close()
            return _to_result(run)

        # ── 6. Resolve entities (coordinator pre-fetches existing) ─────────
        existing_db_entities = list_entities(conn, limit=10000)
        existing_summaries = [
            EntitySummary(
                entity_id=e.id,
                canonical_name=e.canonical_name,
                aliases=e.aliases,
                entity_type=e.type.value,
            )
            for e in existing_db_entities
        ]

        if "resolve_entities" in discovered:
            resolve_result = await client.call_skill_blocking(
                "resolve_entities",
                {
                    "candidate_names": list(all_names),
                    "existing_entities": [s.model_dump(mode="json") for s in existing_summaries],
                },
                traceparent=traceparent,
            )
            resolved_list = [
                ResolvedEntityInfo.model_validate(r)
                for r in resolve_result.get("resolved", [])
            ]
        else:
            # Fallback: pure local resolution without A2A (for tests / partial deploys)
            from mesh_agents.entity_tracker import resolve_entities_pure

            resolved_list = resolve_entities_pure(list(all_names), existing_summaries)

        # Persist new entities
        entity_map: dict[str, str] = {}  # name -> entity_id
        created_count = 0
        for info in resolved_list:
            entity_map[info.name] = info.entity_id
            if info.is_new:
                new_entity = Entity(
                    id=info.entity_id,
                    canonical_name=info.canonical_name,
                    type=EntityType(info.entity_type),
                )
                # Use a try/except for idempotency — another run may have inserted it
                try:
                    from mesh_db.entities import create_entity
                    create_entity(conn, new_entity)
                    created_count += 1
                except Exception:
                    pass  # already exists — hash collision or concurrent insert
        run.entities_created = created_count
        log.info("entities_resolved", total=len(entity_map), created=created_count)

        # ── 7. Insert claims ───────────────────────────────────────────────
        all_resolved_claims: list[ResolvedClaim] = []
        claims_inserted = 0

        # Invert investigation_papers → url -> inv_id so we can tag each
        # claim's investigation lineage in one lookup.
        url_to_investigation_id: dict[str, str] = {}
        for inv_id, urls in investigation_papers.items():
            for u in urls:
                url_to_investigation_id[u] = inv_id

        for paper, extracted_claims, _ in extraction_results:
            for ec in extracted_claims:
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
                create_claim(conn, claim)
                claims_inserted += 1
                # Phase 7a: claim → investigation linkage. If this paper
                # came from an investigate skill dispatch, attach the new
                # claim id so the lifecycle sweep below can resolve.
                inv_id_or_none = url_to_investigation_id.get(paper.source.url)
                if inv_id_or_none is not None:
                    attach_claim_to_investigation(conn, inv_id_or_none, claim.id)
                all_resolved_claims.append(
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

        run.claims_inserted = claims_inserted
        log.info("claims_inserted", count=claims_inserted)

        # ── 7b. Investigation lifecycle (Phase 7a) ────────────────────────
        # Re-read each touched investigation and apply the resolve/abandon
        # rules. Resolve when collected claims clear the threshold; abandon
        # when max_runs elapsed with nothing to show.
        claims_threshold = _investigation_claims_threshold()
        max_runs = _investigation_max_runs()
        for inv in open_investigations:
            current = update_investigation(conn, inv.id)  # re-fetch
            if len(current.collected_claim_ids) >= claims_threshold:
                update_investigation(
                    conn,
                    inv.id,
                    status=InvestigationStatus.resolved,
                    resolved_at=datetime.now(UTC),
                )
                log.info(
                    "investigation_resolved",
                    investigation_id=inv.id,
                    claim_count=len(current.collected_claim_ids),
                )
            elif current.pipeline_runs_attempted >= max_runs:
                update_investigation(
                    conn,
                    inv.id,
                    status=InvestigationStatus.abandoned,
                    resolved_at=datetime.now(UTC),
                )
                log.info(
                    "investigation_abandoned",
                    investigation_id=inv.id,
                    runs_attempted=current.pipeline_runs_attempted,
                )

        # ── 8. SOTA tracking ───────────────────────────────────────────────
        existing_sota = [
            BeliefSummary(
                belief_id=b.id,
                topic=b.topic,
                statement=b.statement,
                confidence=b.confidence,
            )
            for b in list_beliefs(conn, currently_held=True, limit=1000)
            if b.topic.startswith("sota:")
        ]

        if "update_sota" in discovered:
            sota_result = await client.call_skill_blocking(
                "update_sota",
                {
                    "claims": [c.model_dump(mode="json") for c in all_resolved_claims],
                    "existing_sota_beliefs": [b.model_dump(mode="json") for b in existing_sota],
                },
                traceparent=traceparent,
            )
            belief_updates = [
                BeliefUpdate.model_validate(u) for u in sota_result.get("belief_updates", [])
            ]
        else:
            from mesh_agents.sota_tracker import update_sota_pure

            belief_updates = update_sota_pure(all_resolved_claims, existing_sota)

        beliefs_created = 0
        beliefs_revised = 0

        for update in belief_updates:
            if update.is_new_belief:
                belief = Belief(
                    topic=update.topic,
                    statement=update.new_statement,
                    supporting_claim_ids=update.supporting_claim_ids,
                    confidence=update.new_confidence,
                )
                create_belief(conn, belief)
                beliefs_created += 1
                log.info("belief_created", topic=update.topic)
            else:
                assert update.existing_belief_id is not None
                existing_b = get_belief_by_id(conn, update.existing_belief_id)
                if existing_b is None:
                    continue
                revision = BeliefRevision(
                    belief_id=update.existing_belief_id,
                    previous_statement=existing_b.statement,
                    new_statement=update.new_statement,
                    previous_confidence=existing_b.confidence,
                    new_confidence=update.new_confidence,
                    trigger_claim_ids=update.supporting_claim_ids,
                    revised_by_agent="sota_tracker",
                    rationale=update.rationale,
                )
                create_revision(conn, revision)
                update_belief(
                    conn,
                    update.existing_belief_id,
                    statement=update.new_statement,
                    confidence=update.new_confidence,
                    last_revised_at=revision.revised_at,
                    revision_count=existing_b.revision_count + 1,
                )
                beliefs_revised += 1
                log.info("belief_revised", topic=update.topic)

    run.beliefs_created = beliefs_created
    run.beliefs_revised = beliefs_revised
    run.avg_extraction_latency_ms = int(sum(latencies) / len(latencies)) if latencies else 0
    run.errors = errors
    run.finished_at = datetime.now(UTC)

    create_pipeline_run(conn, run)
    conn.close()

    log.info(
        "coordinator_complete",
        sources_inserted=run.sources_inserted,
        claims_inserted=run.claims_inserted,
        entities_created=run.entities_created,
        beliefs_created=beliefs_created,
        beliefs_revised=beliefs_revised,
        errors=len(errors),
    )

    return _to_result(run)


def _to_result(run: PipelineRun) -> PipelineResult:
    return PipelineResult(
        run_id=run.id,
        papers_scouted=run.papers_scouted,
        sources_inserted=run.sources_inserted,
        claims_inserted=run.claims_inserted,
        entities_created=run.entities_created,
        beliefs_created=run.beliefs_created,
        beliefs_revised=run.beliefs_revised,
        avg_extraction_latency_ms=run.avg_extraction_latency_ms,
        errors=[e.model_dump() for e in run.errors],
    )


def parse_since(since_str: str | None) -> datetime | None:
    if since_str is None:
        return None
    if since_str.endswith("h"):
        hours = int(since_str[:-1])
        return datetime.now(UTC) - timedelta(hours=hours)
    if since_str.endswith("d"):
        days = int(since_str[:-1])
        return datetime.now(UTC) - timedelta(days=days)
    return datetime.fromisoformat(since_str)
