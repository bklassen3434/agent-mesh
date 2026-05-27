"""Skeptic sweep — out-of-band falsification orchestrator.

Discovers Curator + Skeptic via A2A capability lookup, asks Curator which
beliefs deserve a challenge, hydrates each pick into a SkepticInput, and
applies the resulting assessments (counter-claims + BeliefRevision) when
the Skeptic's self-reported confidence clears the threshold.

Distinct from the main coordinator on purpose: the falsification flow does
not touch scout/extract/synthesis, only beliefs and revisions. Shares the
A2A discovery boilerplate but not the orchestration shape.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from mesh_a2a.client import MeshA2AClient
from mesh_a2a.tracing import new_traceparent
from mesh_agents.curator import BeliefForCuration, CuratorPick
from mesh_agents.skeptic import (
    HydratedClaim,
    InScopeEntity,
    SkepticAssessment,
    SkepticCounterClaim,
)
from mesh_agents.sota_tracker import BeliefSummary
from mesh_db.agent_tasks import DuckDBTaskRecorder, sweep_orphaned_tasks
from mesh_db.beliefs import get_belief_by_id, list_beliefs, update_belief
from mesh_db.claims import create_claim, get_claims_by_ids
from mesh_db.connection import get_connection
from mesh_db.entities import get_entity_by_id
from mesh_db.migrations import apply_migrations
from mesh_db.pipeline_runs import PipelineRun, create_pipeline_run
from mesh_db.revisions import create_revision, list_revisions
from mesh_db.sources import create_source, get_source_by_id
from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.revision import BeliefRevision
from mesh_models.source import Source, SourceType
from pydantic import BaseModel

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


def _task_resume_threshold() -> int:
    """Seconds an agent_task can sit pending/running before the startup
    sweep considers it orphaned. Default 600s = 10 minutes."""
    return int(os.environ.get("MESH_TASK_RESUME_THRESHOLD", "600"))


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


def _hydrate_claims(conn: Any, ids: list[str]) -> list[HydratedClaim]:
    """Look up claims by ID and project them into Skeptic's hydrated shape."""
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
                status=c.status.value,
            )
        )
    return out


def _collect_in_scope_entities(
    conn: Any, supporting: list[HydratedClaim], contradicting: list[HydratedClaim]
) -> list[InScopeEntity]:
    """Build the entity set Skeptic may reference, deduped by id."""
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
    """One synthetic Source row per assessment, anchoring all counter-claims."""
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


def _persist_assessment(
    conn: Any,
    belief: Belief,
    assessment: SkepticAssessment,
    now: datetime,
) -> tuple[int, int]:
    """Insert source + counter-claims + revision, update belief. Returns (n_claims, n_revisions)."""
    if not assessment.counter_claims:
        # Caller already checked verdict + threshold, but defend against empty
        # counter_claims (could happen for a "weakened" verdict with no specific
        # counter to cite). Without counter-claims there's no trigger evidence,
        # so we skip writing a revision rather than leave a phantom revision.
        return (0, 0)

    source = _make_skeptic_source(belief.id, assessment.rationale, now)
    create_source(conn, source)

    new_claim_ids: list[str] = []
    for cc in assessment.counter_claims:
        claim = _counter_to_claim(cc, source.id)
        create_claim(conn, claim)
        new_claim_ids.append(claim.id)

    # Capture previous state before mutating. We then update the belief FIRST
    # and append the revision second — DuckDB's FK enforcement rejects UPDATEs
    # on rows already referenced by a freshly-inserted row in the same tx, so
    # writing the revision after the update sidesteps that quirk.
    new_confidence = max(0.0, min(1.0, belief.confidence + assessment.suggested_confidence_delta))
    revision = BeliefRevision(
        belief_id=belief.id,
        previous_statement=belief.statement,
        new_statement=belief.statement,  # skeptic does not rewrite the statement; phase 5+ work
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


def _counter_to_claim(cc: SkepticCounterClaim, source_id: str) -> Claim:
    return Claim(
        predicate=cc.predicate,
        subject_entity_id=cc.subject_entity_id,
        object=cc.object,
        source_id=source_id,
        extracted_by_agent="skeptic",
        raw_excerpt=cc.raw_excerpt,
        confidence=cc.confidence,
    )


async def run_skeptic_sweep(db_path: str | None = None) -> SkepticSweepResult:
    """Top-level entry point — `mesh-skeptic-sweep` console script calls this."""
    log.info("skeptic_sweep_starting")

    conn = get_connection(db_path)
    apply_migrations(conn)

    threshold = _apply_threshold()
    pick_count = _pick_count()
    cooldown_days = _cooldown_days()
    now = datetime.now(UTC)
    traceparent = new_traceparent()

    run = PipelineRun(
        run_type="skeptic_sweep",
        started_at=now,
        triggered_by=os.environ.get("MESH_TRIGGERED_BY", "manual"),
    )

    held_beliefs = list_beliefs(conn, currently_held=True, limit=1000)
    log.info("beliefs_considered", count=len(held_beliefs))

    counter_claims_inserted = 0
    revisions_inserted = 0
    assessments_run = 0
    assessments_applied = 0
    picks: list[CuratorPick] = []

    if not held_beliefs:
        run.finished_at = datetime.now(UTC)
        create_pipeline_run(conn, run)
        conn.close()
        return SkepticSweepResult(
            run_id=run.id,
            beliefs_considered=0,
            beliefs_picked=0,
            assessments_run=0,
            assessments_applied=0,
            counter_claims_inserted=0,
            revisions_inserted=0,
        )

    # Phase 6b: persist dispatch lifecycle. Sweep orphans first so the
    # table doesn't keep stale rows from a prior crash.
    sweep_orphaned_tasks(conn, threshold_seconds=_task_resume_threshold())
    recorder = DuckDBTaskRecorder(conn, dispatched_by_run_id=run.id)

    async with MeshA2AClient(task_recorder=recorder) as client:
        discovered = await client.discover(_agent_urls())
        log.info("agents_discovered", skills=list(discovered.keys()))

        for required in ("select_beliefs_to_challenge", "challenge_belief"):
            if required not in discovered:
                raise SystemExit(
                    f"Required skill '{required}' not discovered. "
                    f"Discovered: {list(discovered.keys())}"
                )

        # ── 1. Curator: rank and pick ──────────────────────────────────────
        curator_payload = _build_curator_payload(conn, held_beliefs, now)
        curator_result = await client.call_skill_blocking(
            "select_beliefs_to_challenge",
            {
                "beliefs": [b.model_dump(mode="json") for b in curator_payload],
                "pick_count": pick_count,
                "now": now.isoformat(),
                "cooldown_days": cooldown_days,
            },
            traceparent=traceparent,
        )
        picks = [CuratorPick.model_validate(p) for p in curator_result.get("picks", [])]
        log.info("beliefs_picked", count=len(picks), ids=[p.belief_id for p in picks])

        # ── 2. Skeptic: assess each pick, persist applicable assessments ──
        for pick in picks:
            belief = get_belief_by_id(conn, pick.belief_id)
            if belief is None:
                log.warning("picked_belief_missing", belief_id=pick.belief_id)
                continue
            supporting = _hydrate_claims(conn, belief.supporting_claim_ids)
            contradicting = _hydrate_claims(conn, belief.contradicting_claim_ids)
            in_scope = _collect_in_scope_entities(conn, supporting, contradicting)

            assessment_result = await client.call_skill_blocking(
                "challenge_belief",
                {
                    "belief": BeliefSummary(
                        belief_id=belief.id,
                        topic=belief.topic,
                        statement=belief.statement,
                        confidence=belief.confidence,
                    ).model_dump(mode="json"),
                    "supporting_claims": [c.model_dump(mode="json") for c in supporting],
                    "contradicting_claims": [
                        c.model_dump(mode="json") for c in contradicting
                    ],
                    "in_scope_entities": [e.model_dump(mode="json") for e in in_scope],
                },
                traceparent=traceparent,
            )
            assessment = SkepticAssessment(
                verdict=assessment_result["verdict"],
                confidence=float(assessment_result["confidence"]),
                rationale=assessment_result["rationale"],
                suggested_confidence_delta=float(
                    assessment_result.get("suggested_confidence_delta", 0.0)
                ),
                counter_claims=[
                    SkepticCounterClaim.model_validate(c)
                    for c in assessment_result.get("counter_claims", [])
                ],
            )
            assessments_run += 1
            log.info(
                "skeptic_assessment",
                belief_id=belief.id,
                verdict=assessment.verdict,
                confidence=assessment.confidence,
                counter_claim_count=len(assessment.counter_claims),
            )

            if (
                assessment.verdict in {"weakened", "contradicted"}
                and assessment.confidence >= threshold
            ):
                n_claims, n_revs = _persist_assessment(
                    conn, belief, assessment, datetime.now(UTC)
                )
                counter_claims_inserted += n_claims
                revisions_inserted += n_revs
                if n_revs:
                    assessments_applied += 1

    run.claims_inserted = counter_claims_inserted
    run.beliefs_revised = revisions_inserted
    run.sources_inserted = revisions_inserted  # one synthetic source per applied assessment
    run.finished_at = datetime.now(UTC)
    create_pipeline_run(conn, run)
    conn.close()

    log.info(
        "skeptic_sweep_complete",
        beliefs_considered=len(held_beliefs),
        beliefs_picked=len(picks),
        assessments_run=assessments_run,
        assessments_applied=assessments_applied,
        counter_claims_inserted=counter_claims_inserted,
        revisions_inserted=revisions_inserted,
    )

    return SkepticSweepResult(
        run_id=run.id,
        beliefs_considered=len(held_beliefs),
        beliefs_picked=len(picks),
        assessments_run=assessments_run,
        assessments_applied=assessments_applied,
        counter_claims_inserted=counter_claims_inserted,
        revisions_inserted=revisions_inserted,
    )


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
