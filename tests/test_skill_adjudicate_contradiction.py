"""Deep skill ``adjudicate-contradiction``: the across-rounds state machine.

Proves the two steps of the loop and its termination guarantee:

* **plan/gather** — with no adjudication investigation yet, the skill opens exactly
  one ``origin=adjudication`` investigation tagged to the belief (and makes no LLM
  call, emits no revision);
* **reason/decide** — once that investigation has terminated, the skill weighs the
  belief (shared skeptic core, mocked) and emits exactly one ``adjudicator``
  revision that *cites the fresh contradicting claim* — which is what later marks
  the contradiction adjudicated so the producer stops re-firing;
* **suppression** — while a gather investigation is still open it does nothing;
* a refutation that collapses the belief drops it from the held set append-only.

Like every skill it returns Effects and never writes; the gateway persists them.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from mesh_agents.skeptic import SkepticAssessment
from mesh_agents.skill import Skill, get_skill, register_skill
from mesh_agents.skills.adjudicate_contradiction import AdjudicateContradictionSkill
from mesh_db.beliefs import create_belief, get_belief_by_id
from mesh_db.claims import create_claim
from mesh_db.effects import apply_effects
from mesh_db.entities import create_entity
from mesh_db.investigations import create_investigation, list_investigations
from mesh_db.revisions import list_revisions
from mesh_db.sources import create_source
from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.effect import OpenInvestigationEffect, ReviseBeliefEffect
from mesh_models.entity import Entity, EntityType
from mesh_models.investigation import Investigation, InvestigationOrigin, InvestigationStatus
from mesh_models.source import Source, SourceType
from mesh_models.tension import ReasoningTier, Tension, TensionKind

_FIELD = "ai-robotics"


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


def _seed_contradicted_belief(conn: Any) -> tuple[Belief, str]:
    """A confident, load-bearing held belief plus one fresh contradicting claim."""
    entity = Entity(canonical_name="TestModel-7B", type=EntityType.model)
    create_entity(conn, entity)
    source = Source(
        type=SourceType.arxiv,
        url="https://arxiv.org/abs/2023.06.0001",
        published_at=datetime(2023, 6, 15, tzinfo=UTC),
        raw_content_hash="hash-seed",
    )
    create_source(conn, source)

    support = Claim(
        predicate="achieves_score",
        subject_entity_id=entity.id,
        object={"score": 87.5, "benchmark": "MMLU"},
        source_id=source.id,
        extracted_by_agent="claim_extractor",
        raw_excerpt="TestModel-7B achieves 87.5% on MMLU",
        confidence=0.95,
    )
    create_claim(conn, support)
    support2 = Claim(
        predicate="achieves_score",
        subject_entity_id=entity.id,
        object={"score": 87.0, "benchmark": "MMLU"},
        source_id=source.id,
        extracted_by_agent="claim_extractor",
        raw_excerpt="reproduced at ~87%",
        confidence=0.9,
    )
    create_claim(conn, support2)
    contra = Claim(
        predicate="achieves_score",
        subject_entity_id=entity.id,
        object={"score": 71.0, "benchmark": "MMLU"},
        source_id=source.id,
        extracted_by_agent="skeptic",
        raw_excerpt="A re-evaluation reports only 71% on MMLU",
        confidence=0.8,
    )
    create_claim(conn, contra)

    belief = Belief(
        topic="sota:MMLU",
        statement="TestModel-7B achieves 87.5% on MMLU",
        supporting_claim_ids=[support.id, support2.id],
        contradicting_claim_ids=[contra.id],
        confidence=0.85,
        is_currently_held=True,
    )
    create_belief(conn, belief)
    return belief, contra.id


def _tension(belief: Belief, contra_id: str) -> Tension:
    return Tension(
        id=f"contradicted_belief:{belief.id}",
        field_id=_FIELD,
        kind=TensionKind.contradicted_belief,
        subject=belief.topic,
        rationale="contradicted load-bearing belief",
        value=0.9,
        est_cost_usd=0.08,
        handler_skill="adjudicate-contradiction",
        tier=ReasoningTier.deep,
        target_ref={"belief_id": belief.id},
        signals={"contradicting_claim_ids": [contra_id], "dependents": 2, "confidence": 0.85},
    )


def _assessment(verdict: str, delta: float) -> SkepticAssessment:
    return SkepticAssessment(
        verdict=verdict,  # type: ignore[arg-type]
        confidence=0.85,
        rationale="weighed corroboration against the contradiction",
        suggested_confidence_delta=delta,
        counter_claims=[],
    )


def test_skill_metadata_and_registration() -> None:
    skill = AdjudicateContradictionSkill()
    assert skill.skill_id == "adjudicate-contradiction"
    assert TensionKind.contradicted_belief in skill.handles
    assert isinstance(skill, Skill)
    if get_skill("adjudicate-contradiction") is None:
        register_skill(AdjudicateContradictionSkill)
    assert get_skill("adjudicate-contradiction") is not None


def test_plan_step_opens_one_adjudication_investigation(tmp_db: Any) -> None:
    belief, contra_id = _seed_contradicted_belief(tmp_db)
    skill = AdjudicateContradictionSkill(llm=_MockLLM(_assessment("contradicted", -0.5)))

    effects = asyncio.run(skill.run(tmp_db, _tension(belief, contra_id), budget_usd=0.08))

    assert len(effects) == 1
    eff = effects[0]
    assert isinstance(eff, OpenInvestigationEffect)
    assert eff.investigation.origin is InvestigationOrigin.adjudication
    assert eff.investigation.opened_by_belief_id == belief.id
    assert eff.investigation.hypothesis == belief.statement
    # The skill wrote nothing; the gateway opens it.
    assert list_investigations(tmp_db, field_id=_FIELD) == []
    apply_effects(tmp_db, effects)
    opened = list_investigations(tmp_db, origin=InvestigationOrigin.adjudication, field_id=_FIELD)
    assert len(opened) == 1


def test_gather_in_flight_suppresses_the_skill(tmp_db: Any) -> None:
    belief, contra_id = _seed_contradicted_belief(tmp_db)
    create_investigation(
        tmp_db,
        Investigation(
            question="gather",
            opened_by_belief_id=belief.id,
            origin=InvestigationOrigin.adjudication,
            status=InvestigationStatus.in_progress,
        ),
        field_id=_FIELD,
    )
    skill = AdjudicateContradictionSkill(llm=_MockLLM(_assessment("contradicted", -0.5)))
    # A gather is still running → never act mid-gather.
    assert asyncio.run(skill.run(tmp_db, _tension(belief, contra_id), budget_usd=0.08)) == []


def test_decide_step_emits_one_adjudicator_revision_citing_the_contradiction(tmp_db: Any) -> None:
    belief, contra_id = _seed_contradicted_belief(tmp_db)
    create_investigation(
        tmp_db,
        Investigation(
            question="gather",
            opened_by_belief_id=belief.id,
            origin=InvestigationOrigin.adjudication,
            status=InvestigationStatus.resolved,
        ),
        field_id=_FIELD,
    )
    skill = AdjudicateContradictionSkill(llm=_MockLLM(_assessment("weakened", -0.2)))

    effects = asyncio.run(skill.run(tmp_db, _tension(belief, contra_id), budget_usd=0.08))

    assert len(effects) == 1
    rev = effects[0]
    assert isinstance(rev, ReviseBeliefEffect)
    assert rev.revised_by_agent == "adjudicator"
    # Cites the fresh contradiction — the termination signal the producer keys off.
    assert rev.trigger_claim_ids == [contra_id]
    assert rev.recompute_confidence is False
    assert rev.new_confidence == pytest.approx(0.65)
    assert rev.set_not_held is False

    # Gateway applies it append-only.
    report = apply_effects(tmp_db, effects)
    assert report.errors == []
    assert report.beliefs_revised == 1
    stored = get_belief_by_id(tmp_db, belief.id)
    assert stored is not None
    assert stored.confidence == pytest.approx(0.65)
    assert stored.is_currently_held is True
    revs = list_revisions(tmp_db, belief_id=belief.id)
    assert len(revs) == 1
    assert revs[0].revised_by_agent == "adjudicator"


def test_refutation_collapses_belief_out_of_held_set_append_only(tmp_db: Any) -> None:
    belief, contra_id = _seed_contradicted_belief(tmp_db)
    create_investigation(
        tmp_db,
        Investigation(
            question="gather",
            opened_by_belief_id=belief.id,
            origin=InvestigationOrigin.adjudication,
            status=InvestigationStatus.resolved,
        ),
        field_id=_FIELD,
    )
    # Confidence 0.85 - 0.8 = 0.05, below the 0.2 refute floor → drops from held set.
    skill = AdjudicateContradictionSkill(llm=_MockLLM(_assessment("contradicted", -0.8)))

    effects = asyncio.run(skill.run(tmp_db, _tension(belief, contra_id), budget_usd=0.08))
    rev = effects[0]
    assert isinstance(rev, ReviseBeliefEffect)
    assert rev.set_not_held is True

    apply_effects(tmp_db, effects)
    stored = get_belief_by_id(tmp_db, belief.id)
    assert stored is not None
    assert stored.is_currently_held is False  # dropped...
    # ...but append-only: the row and its revision survive.
    assert len(list_revisions(tmp_db, belief_id=belief.id)) == 1


def test_missing_belief_ref_returns_empty(tmp_db: Any) -> None:
    skill = AdjudicateContradictionSkill(llm=_MockLLM(_assessment("weakened", -0.1)))
    bare = Tension(
        id="contradicted_belief:x",
        field_id=_FIELD,
        kind=TensionKind.contradicted_belief,
        subject="x",
        rationale="x",
        value=0.9,
        est_cost_usd=0.08,
        handler_skill="adjudicate-contradiction",
        tier=ReasoningTier.deep,
        target_ref={},
    )
    assert asyncio.run(skill.run(tmp_db, bare, budget_usd=0.08)) == []
