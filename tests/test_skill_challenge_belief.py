"""Phase 2 skill ``challenge-belief``: bid → run → Effects → gateway.

Proves the skill wraps the existing skeptic and translates its assessment into
exactly the writes ``skeptic_sweep.py`` performs today — a synthetic source, one
counter-claim per assessment counter-claim, and an append-only belief revision —
while never touching the DB itself (it returns Effects; the gateway writes).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mesh_agents.skeptic import SkepticAssessment, SkepticCounterClaim
from mesh_agents.skill import Skill, get_skill, register_skill
from mesh_agents.skills.challenge_belief import ChallengeBeliefSkill
from mesh_db.beliefs import create_belief, get_belief_by_id
from mesh_db.claims import create_claim, list_claims
from mesh_db.effects import apply_effects
from mesh_db.entities import create_entity
from mesh_db.revisions import list_revisions
from mesh_db.sources import create_source, list_sources
from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.effect import (
    CreateClaimEffect,
    CreateSourceEffect,
    ReviseBeliefEffect,
)
from mesh_models.entity import Entity, EntityType
from mesh_models.source import Source, SourceType
from mesh_models.tension import Tension, TensionKind


class _MockLLM:
    """Returns a fixed ``SkepticAssessment`` from ``complete_with_usage``."""

    model = "mock-model"

    def __init__(self, assessment: SkepticAssessment) -> None:
        self._assessment = assessment

    def complete_with_usage(
        self,
        name: str,
        system: str,
        user: str,
        response_model: type | None = None,
        options: object | None = None,
    ) -> tuple[object, int, object]:
        from mesh_llm import LLMUsage

        return self._assessment, 500, LLMUsage(input_tokens=100, output_tokens=50)


def _seed_belief(conn: Any) -> tuple[Belief, str]:
    """Seed entity + source + supporting claim + a stale held belief."""
    entity = Entity(canonical_name="TestModel-7B", type=EntityType.model)
    create_entity(conn, entity)

    source = Source(
        type=SourceType.arxiv,
        url="https://arxiv.org/abs/2023.06.0001",
        published_at=datetime(2023, 6, 15, tzinfo=UTC),
        raw_content_hash="hash-seed",
    )
    create_source(conn, source)

    claim = Claim(
        predicate="achieves_score",
        subject_entity_id=entity.id,
        object={"score": 87.5, "benchmark": "MMLU"},
        source_id=source.id,
        extracted_by_agent="claim_extractor",
        raw_excerpt="TestModel-7B achieves 87.5% on MMLU",
        confidence=0.95,
    )
    create_claim(conn, claim)

    belief = Belief(
        topic="sota:MMLU",
        statement="TestModel-7B achieves 87.5% on MMLU",
        supporting_claim_ids=[claim.id],
        confidence=0.7,
        is_currently_held=True,
        last_revised_at=datetime.now(UTC) - timedelta(days=120),
    )
    create_belief(conn, belief)
    return belief, entity.id


def _tension(belief_id: str) -> Tension:
    return Tension(
        id=f"stale_belief:{belief_id}",
        field_id="ai-robotics",
        kind=TensionKind.stale_belief,
        subject="sota:MMLU",
        rationale="no fresh evidence in 120 days",
        value=0.6,
        est_cost_usd=0.04,
        handler_skill="challenge-belief",
        target_ref={"belief_id": belief_id},
    )


def _weakening_assessment(entity_id: str) -> SkepticAssessment:
    return SkepticAssessment(
        verdict="weakened",
        confidence=0.85,
        rationale="The benchmark result is stale and uncorroborated.",
        suggested_confidence_delta=-0.15,
        counter_claims=[
            SkepticCounterClaim(
                predicate="achieves_score",
                subject_entity_id=entity_id,
                object={"score": 81.2, "benchmark": "MMLU"},
                raw_excerpt="A re-evaluation reports only 81.2% on MMLU",
                confidence=0.7,
            )
        ],
    )


def test_skill_metadata_and_registration() -> None:
    skill = ChallengeBeliefSkill()
    assert skill.skill_id == "challenge-belief"
    assert TensionKind.contested_claim in skill.handles
    assert TensionKind.stale_belief in skill.handles
    assert isinstance(skill, Skill)  # satisfies the runtime-checkable Protocol

    # Importing the module ran @register_skill; tolerate a prior clear_registry().
    if get_skill("challenge-belief") is None:
        register_skill(ChallengeBeliefSkill)
    assert get_skill("challenge-belief") is not None


def test_run_emits_effects_and_gateway_writes(tmp_db: Any) -> None:
    belief, entity_id = _seed_belief(tmp_db)
    skill = ChallengeBeliefSkill(llm=_MockLLM(_weakening_assessment(entity_id)))

    effects = asyncio.run(skill.run(tmp_db, _tension(belief.id), budget_usd=0.04))

    # Shape: one synthetic source, one counter-claim, one revision — all scoped
    # to the tension's field. The skill wrote nothing itself.
    kinds = [type(e) for e in effects]
    assert kinds == [CreateSourceEffect, CreateClaimEffect, ReviseBeliefEffect]
    src, clm, rev = effects
    assert isinstance(src, CreateSourceEffect)
    assert src.field_id == "ai-robotics"
    assert src.source.type == SourceType.agent_reasoning
    assert isinstance(clm, CreateClaimEffect)
    assert clm.field_id == "ai-robotics"
    assert clm.claim.extracted_by_agent == "skeptic"
    assert clm.claim.source_id == src.source.id
    assert isinstance(rev, ReviseBeliefEffect)
    assert rev.revised_by_agent == "skeptic"
    assert rev.trigger_claim_ids == [clm.claim.id]
    assert rev.new_confidence == pytest.approx(0.55)

    # Skill itself never wrote: only the supporting claim + arxiv source exist yet.
    assert len(list_claims(tmp_db)) == 1
    assert len(list_sources(tmp_db)) == 1

    # Now the gateway applies the intents.
    report = apply_effects(tmp_db, effects)
    assert report.errors == []
    assert report.sources_created == 1
    assert report.claims_created == 1
    assert report.beliefs_revised == 1

    stored = get_belief_by_id(tmp_db, belief.id)
    assert stored is not None
    assert stored.confidence == pytest.approx(0.55)
    assert stored.statement == belief.statement  # skeptic never rewrites it
    assert stored.revision_count == 1

    revs = list_revisions(tmp_db, belief_id=belief.id)
    assert len(revs) == 1
    assert revs[0].revised_by_agent == "skeptic"
    assert revs[0].previous_confidence == pytest.approx(0.7)
    assert revs[0].new_confidence == pytest.approx(0.55)


def test_contradicted_folds_counter_claims_into_contradicting_set(tmp_db: Any) -> None:
    belief, entity_id = _seed_belief(tmp_db)
    assessment = _weakening_assessment(entity_id)
    assessment.verdict = "contradicted"
    skill = ChallengeBeliefSkill(llm=_MockLLM(assessment))

    effects = asyncio.run(skill.run(tmp_db, _tension(belief.id), budget_usd=0.04))
    apply_effects(tmp_db, effects)

    claim_effect = next(e for e in effects if isinstance(e, CreateClaimEffect))
    stored = get_belief_by_id(tmp_db, belief.id)
    assert stored is not None
    assert claim_effect.claim.id in stored.contradicting_claim_ids


def test_below_threshold_or_supported_emits_nothing(tmp_db: Any) -> None:
    belief, entity_id = _seed_belief(tmp_db)

    # Supported verdict — belief holds up, no writes.
    supported = SkepticAssessment(
        verdict="supported", confidence=0.9, rationale="holds up", counter_claims=[]
    )
    skill = ChallengeBeliefSkill(llm=_MockLLM(supported))
    assert asyncio.run(skill.run(tmp_db, _tension(belief.id), budget_usd=0.04)) == []

    # Weakened but below the apply-threshold — also no phantom write.
    weak = _weakening_assessment(entity_id)
    weak.confidence = 0.5
    skill = ChallengeBeliefSkill(llm=_MockLLM(weak))
    assert asyncio.run(skill.run(tmp_db, _tension(belief.id), budget_usd=0.04)) == []


def test_missing_belief_ref_returns_empty(tmp_db: Any) -> None:
    skill = ChallengeBeliefSkill(llm=_MockLLM(_weakening_assessment("ent-x")))

    # No belief_id in target_ref.
    bare = _tension("unused")
    bare.target_ref = {}
    assert asyncio.run(skill.run(tmp_db, bare, budget_usd=0.04)) == []

    # belief_id present but the belief doesn't exist.
    assert asyncio.run(
        skill.run(tmp_db, _tension("does-not-exist"), budget_usd=0.04)
    ) == []
