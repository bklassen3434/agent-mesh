from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import structlog
from mesh_agents.arxiv_scout import ArxivScoutAgent, ArxivScoutInput, ScoutedPaper
from mesh_agents.claim_extractor import ClaimExtractorAgent, ClaimExtractorInput, ExtractedClaim
from mesh_agents.entity_tracker import EntityTrackerAgent, EntityTrackerInput
from mesh_agents.sota_tracker import ResolvedClaim, SotaTrackerAgent, SotaTrackerInput
from mesh_db.beliefs import create_belief, get_belief_by_id, update_belief
from mesh_db.claims import create_claim
from mesh_db.connection import get_connection
from mesh_db.migrations import apply_migrations
from mesh_db.pipeline_runs import PipelineError, PipelineRun, create_pipeline_run
from mesh_db.revisions import create_revision
from mesh_db.sources import create_source, list_sources
from mesh_llm.client import OllamaClient, OllamaNotReadyError
from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.revision import BeliefRevision
from pydantic import BaseModel

log = structlog.get_logger()


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


async def run_pipeline(
    categories: list[str],
    max_papers: int,
    since: datetime | None,
    db_path: str | None = None,
) -> PipelineResult:
    llm = OllamaClient()

    log.info("pipeline_starting", model=llm.model, categories=categories, max_papers=max_papers)

    # 0. Fail fast if Ollama isn't ready
    try:
        llm.health_check()
    except OllamaNotReadyError as exc:
        raise SystemExit(f"Ollama not ready: {exc}") from exc

    conn = get_connection(db_path)
    apply_migrations(conn)

    run = PipelineRun(started_at=datetime.now(UTC))
    errors: list[PipelineError] = []
    latencies: list[int] = []

    # 1. Scout arxiv
    scout = ArxivScoutAgent()
    scout_output = await scout.run(
        ArxivScoutInput(categories=categories, max_results=max_papers, since=since)
    )
    papers = scout_output.papers
    log.info("papers_scouted", count=len(papers))
    run.papers_scouted = len(papers)

    # 2. Filter by raw_content_hash to avoid duplicate sources
    existing_hashes: set[str] = {
        s.raw_content_hash for s in list_sources(conn, limit=10000)
    }
    new_papers = [p for p in papers if p.source.raw_content_hash not in existing_hashes]
    log.info("new_papers_after_dedup", count=len(new_papers), skipped=len(papers) - len(new_papers))

    # 3. Insert new sources
    for paper in new_papers:
        create_source(conn, paper.source)
    run.sources_inserted = len(new_papers)

    if not new_papers:
        run.finished_at = datetime.now(UTC)
        create_pipeline_run(conn, run)
        conn.close()
        return _to_result(run)

    # 4. Extract claims concurrently (bounded concurrency)
    semaphore = asyncio.Semaphore(_get_concurrency())
    extractor = ClaimExtractorAgent(llm=llm)

    extraction_results: list[tuple[ScoutedPaper, list[ExtractedClaim], int]] = []

    async def extract_one(paper: ScoutedPaper) -> None:
        async with semaphore:
            try:
                output = await extractor.run(ClaimExtractorInput(paper=paper))
                extraction_results.append((paper, output.claims, output.latency_ms))
                log.info(
                    "claims_extracted",
                    arxiv_id=paper.arxiv_id,
                    count=len(output.claims),
                    latency_ms=output.latency_ms,
                )
                if output.latency_ms > 0:
                    latencies.append(output.latency_ms)
            except OllamaNotReadyError:
                raise
            except Exception as exc:
                errors.append(
                    PipelineError(
                        paper_id=paper.arxiv_id,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                )
                log.warning("extraction_failed", arxiv_id=paper.arxiv_id, error=str(exc))

    await asyncio.gather(*(extract_one(p) for p in new_papers))

    # 5. Collect all entity names across all extractions
    all_names: set[str] = set()
    for _, claims, _ in extraction_results:
        for extracted in claims:
            all_names.add(extracted.subject_name)

    # 6. Resolve/create entities
    tracker = EntityTrackerAgent(db_conn=conn)
    tracker_output = await tracker.run(EntityTrackerInput(names=list(all_names)))
    run.entities_created = tracker_output.created_count
    log.info(
        "entities_resolved",
        total=len(tracker_output.resolved),
        created=tracker_output.created_count,
    )

    # 7. Insert claims and build ResolvedClaims for SOTA tracker
    all_resolved_claims: list[ResolvedClaim] = []
    claims_inserted = 0

    for paper, extracted_claims, _ in extraction_results:
        for ec in extracted_claims:
            entity_id = tracker_output.resolved.get(ec.subject_name)
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

    # 8. SOTA tracking
    sota_tracker = SotaTrackerAgent(db_conn=conn)
    sota_output = await sota_tracker.run(
        SotaTrackerInput(claims_with_resolved_entities=all_resolved_claims)
    )

    beliefs_created = 0
    beliefs_revised = 0

    for update in sota_output.belief_updates:
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
            existing = get_belief_by_id(conn, update.existing_belief_id)
            if existing is None:
                continue
            revision = BeliefRevision(
                belief_id=update.existing_belief_id,
                previous_statement=existing.statement,
                new_statement=update.new_statement,
                previous_confidence=existing.confidence,
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
                revision_count=existing.revision_count + 1,
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
        "pipeline_complete",
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
