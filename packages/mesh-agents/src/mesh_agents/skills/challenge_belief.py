"""Phase 2 skill: ``challenge-belief`` — attack a belief to see if it holds up.

The skill is the agentic unit: it handles a contested/stale belief, calls the
shared skeptic core (``challenge_belief_with_memory`` — prompt + LLM + structured
output + injected memory) directly against the belief's evidence, and translates
the resulting :class:`SkepticAssessment` into ``Effect``s — **never writing
itself**. (``SkepticAgent`` is now only the orphaned A2A adapter over that same
core.)

The effect translation reproduces, intent-for-intent, what
``apps/pipeline/skeptic_sweep.py`` persists today (``_persist_assessment`` +
``_assessment_verdict``):

* gate on the apply-threshold — only a ``weakened``/``contradicted`` verdict whose
  confidence clears ``MESH_SKEPTIC_APPLY_THRESHOLD`` *and* that carries at least
  one in-scope counter-claim produces any writes (a phantom revision is never
  emitted);
* one synthetic ``agent_reasoning`` source (``CreateSourceEffect``) carrying the
  skeptic's rationale, then one ``CreateClaimEffect`` per counter-claim
  (``extracted_by_agent="skeptic"``, pointed at that source);
* one ``ReviseBeliefEffect`` (``revised_by_agent="skeptic"``) recording the
  confidence delta — the statement is left unchanged (the skeptic never rewrites
  it), and a ``contradicted`` verdict folds the new counter-claims into the
  belief's contradicting set.

Everything is scoped to ``tension.field_id``; the gateway
(``mesh_db.effects.apply_effects``) owns the append-only revision write.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, datetime
from typing import Any

from mesh_llm import make_routed_llm_client
from mesh_models.claim import Claim
from mesh_models.effect import (
    CreateClaimEffect,
    CreateSourceEffect,
    Effect,
    ReviseBeliefEffect,
)
from mesh_models.source import Source, SourceType
from mesh_models.tension import Tension, TensionKind

from mesh_agents.skeptic import (
    HydratedClaim,
    InScopeEntity,
    SkepticAssessment,
    SkepticCounterClaim,
    SkepticInput,
    challenge_belief_with_memory,
)
from mesh_agents.skill import register_skill
from mesh_agents.sota_tracker import BeliefSummary

# Rough cost estimate for one belief challenge (one skeptic LLM call).
_EST_COST_USD = 0.04


def _apply_threshold() -> float:
    """Confidence an assessment must clear to write — mirrors skeptic_sweep."""
    return float(os.environ.get("MESH_SKEPTIC_APPLY_THRESHOLD", "0.7"))


def _source_reliability() -> float:
    return float(os.environ.get("MESH_SKEPTIC_SOURCE_RELIABILITY", "0.4"))


# ── hydration (belief → SkepticInput) ────────────────────────────────────────
# Mirrors skeptic_sweep's read-only hydration; lives here so the skill never
# depends on apps/pipeline (the one-way import rule).


def _hydrate_claims(conn: Any, ids: list[str]) -> list[HydratedClaim]:
    if not ids:
        return []
    from mesh_db.claims import get_claims_by_ids
    from mesh_db.sources import get_source_by_id

    out: list[HydratedClaim] = []
    for c in get_claims_by_ids(conn, ids):
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
    from mesh_db.entities import get_entity_by_id

    ids = {c.subject_entity_id for c in supporting + contradicting}
    out: list[InScopeEntity] = []
    for eid in ids:
        ent = get_entity_by_id(conn, eid)
        if ent is None:
            continue
        out.append(
            InScopeEntity(
                entity_id=ent.id, canonical_name=ent.canonical_name, type=ent.type.value
            )
        )
    return out


# ── assessment → effects (mirrors _persist_assessment) ───────────────────────


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


def _assessment_to_effects(
    belief: Any, assessment: SkepticAssessment, field_id: str, now: datetime
) -> list[Effect]:
    """Translate a skeptic assessment into the writes skeptic_sweep performs.

    Returns ``[]`` unless the verdict clears the apply-threshold and carries at
    least one counter-claim (no phantom revision, exactly as today)."""
    if assessment.verdict not in {"weakened", "contradicted"}:
        return []
    if assessment.confidence < _apply_threshold():
        return []
    if not assessment.counter_claims:
        return []

    source = _make_skeptic_source(belief.id, assessment.rationale, now)
    effects: list[Effect] = [CreateSourceEffect(field_id=field_id, source=source)]

    new_claim_ids: list[str] = []
    for cc in assessment.counter_claims:
        claim = _counter_to_claim(cc, source.id)
        effects.append(CreateClaimEffect(field_id=field_id, claim=claim))
        new_claim_ids.append(claim.id)

    new_confidence = max(
        0.0, min(1.0, belief.confidence + assessment.suggested_confidence_delta)
    )
    contradicting: list[str] | None = None
    if assessment.verdict == "contradicted":
        contradicting = list(belief.contradicting_claim_ids) + new_claim_ids

    effects.append(
        ReviseBeliefEffect(
            belief_id=belief.id,
            new_statement=belief.statement,  # skeptic does not rewrite the statement
            new_confidence=new_confidence,
            revised_by_agent="skeptic",
            rationale=assessment.rationale,
            trigger_claim_ids=new_claim_ids,
            contradicting_claim_ids=contradicting,
        )
    )
    return effects


@register_skill
class ChallengeBeliefSkill:
    """Attack a held belief with the existing skeptic and emit the resulting
    counter-claims + confidence revision as Effects (never a direct write)."""

    skill_id = "challenge-belief"
    handles = (TensionKind.contested_claim, TensionKind.stale_belief)

    def __init__(self, llm: Any | None = None) -> None:
        # ``llm`` is injectable for tests; in production it's built lazily in
        # ``run`` (the registry instantiates skills no-arg at import time).
        self._llm = llm

    async def run(
        self, conn: Any, tension: Tension, *, budget_usd: float
    ) -> list[Effect]:
        from mesh_db.beliefs import get_belief_by_id

        belief_id = tension.target_ref.get("belief_id")
        if not belief_id:
            return []
        belief = get_belief_by_id(conn, belief_id)
        if belief is None:
            return []

        supporting = _hydrate_claims(conn, list(belief.supporting_claim_ids))
        contradicting = _hydrate_claims(conn, list(belief.contradicting_claim_ids))
        skeptic_input = SkepticInput(
            belief=BeliefSummary(
                belief_id=belief.id,
                topic=belief.topic,
                statement=belief.statement,
                confidence=belief.confidence,
            ),
            supporting_claims=supporting,
            contradicting_claims=contradicting,
            in_scope_entities=_collect_in_scope_entities(
                conn, supporting, contradicting
            ),
        )

        # The skill is the agentic unit: call the shared challenge core (prompt +
        # LLM + structured output + injected memory, on this skill's connection)
        # directly. A parse failure resolves to an inconclusive assessment inside
        # the core, so no extra guard is needed here.
        llm = self._llm or make_routed_llm_client(agent_name="skeptic")
        assessment, _usage, _model = await asyncio.to_thread(
            challenge_belief_with_memory, llm, skeptic_input, "skeptic", tension.field_id, conn
        )
        return _assessment_to_effects(
            belief, assessment, tension.field_id, datetime.now(UTC)
        )
