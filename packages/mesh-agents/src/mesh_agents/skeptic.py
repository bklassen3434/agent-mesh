"""Skeptic agent — challenges existing beliefs by finding evidence problems.

Pure function over typed input; emits a SkepticAssessment that the coordinator
(skeptic_sweep) materializes into counter-claims, a synthetic source, and a
BeliefRevision. The agent never touches the DB.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from mesh_a2a.card_builder import build_agent_card
from mesh_a2a.task_server import build_task_app
from mesh_llm import LLMClient, LLMProviderNotReadyError, LLMResponseError
from mesh_llm.prompts import SKEPTIC_SYSTEM, format_skeptic_user
from mesh_models.claim import FailureMode
from pydantic import BaseModel, Field
from starlette.applications import Starlette

from mesh_agents.base import BaseAgent
from mesh_agents.sota_tracker import BeliefSummary

logger = logging.getLogger(__name__)


Verdict = Literal["supported", "weakened", "contradicted", "inconclusive"]
CounterPredicate = Literal["achieves_score", "outperforms", "developed_by", "evaluated_on"]


class InScopeEntity(BaseModel):
    entity_id: str
    canonical_name: str
    type: str


class HydratedClaim(BaseModel):
    claim_id: str
    predicate: str
    subject_entity_id: str
    object: dict[str, Any]
    raw_excerpt: str
    confidence: float
    source_url: str | None = None
    source_published_at: datetime | None = None
    status: str = "active"


class SkepticCounterClaim(BaseModel):
    predicate: CounterPredicate
    subject_entity_id: str
    object: dict[str, Any]
    raw_excerpt: str
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    # Phase 7 pre-work: structured taxonomy alongside the free-text
    # rationale. The Skeptic LLM must pick one when emitting a
    # counter-claim. `other` is the fallback when none fit.
    failure_mode: FailureMode = Field(default=FailureMode.other)


class SkepticAssessment(BaseModel):
    verdict: Verdict
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    suggested_confidence_delta: float = Field(ge=-1.0, le=1.0, default=0.0)
    counter_claims: list[SkepticCounterClaim] = Field(default_factory=list)


class SkepticInput(BaseModel):
    belief: BeliefSummary
    supporting_claims: list[HydratedClaim] = Field(default_factory=list)
    contradicting_claims: list[HydratedClaim] = Field(default_factory=list)
    in_scope_entities: list[InScopeEntity] = Field(default_factory=list)


# Phase 2 A2A skill types ----------------------------------------------------


class ChallengeBeliefSkillInput(BaseModel):
    belief: dict[str, Any]
    supporting_claims: list[dict[str, Any]] = Field(default_factory=list)
    contradicting_claims: list[dict[str, Any]] = Field(default_factory=list)
    in_scope_entities: list[dict[str, Any]] = Field(default_factory=list)


class ChallengeBeliefSkillOutput(BaseModel):
    verdict: Verdict
    confidence: float
    rationale: str
    suggested_confidence_delta: float
    counter_claims: list[dict[str, Any]]


# Shared assessment logic ----------------------------------------------------


_INCONCLUSIVE = SkepticAssessment(
    verdict="inconclusive",
    confidence=0.0,
    rationale="LLM response could not be parsed.",
    suggested_confidence_delta=0.0,
    counter_claims=[],
)


def _format_claim_block(claims: list[HydratedClaim]) -> str:
    if not claims:
        return ""
    lines: list[str] = []
    for c in claims:
        published = c.source_published_at.isoformat() if c.source_published_at else "unknown"
        lines.append(
            f"- [{c.claim_id}] predicate={c.predicate} subject_entity_id={c.subject_entity_id} "
            f"object={json.dumps(c.object, default=str)} status={c.status} "
            f"source_published_at={published} confidence={c.confidence:.2f} "
            f"excerpt={c.raw_excerpt!r}"
        )
    return "\n".join(lines)


def _format_entities_block(entities: list[InScopeEntity]) -> str:
    if not entities:
        return ""
    return "\n".join(
        f"- entity_id={e.entity_id} canonical_name={e.canonical_name!r} type={e.type}"
        for e in entities
    )


def _filter_to_scope(
    assessment: SkepticAssessment, entities: list[InScopeEntity]
) -> SkepticAssessment:
    """Drop any counter-claims that reference an out-of-scope entity_id."""
    if not assessment.counter_claims:
        return assessment
    allowed = {e.entity_id for e in entities}
    kept = [c for c in assessment.counter_claims if c.subject_entity_id in allowed]
    if len(kept) == len(assessment.counter_claims):
        return assessment
    logger.warning(
        "skeptic_dropped_out_of_scope_claims",
        extra={
            "dropped": len(assessment.counter_claims) - len(kept),
            "kept": len(kept),
        },
    )
    return assessment.model_copy(update={"counter_claims": kept})


def _assess_sync(llm: LLMClient, input: SkepticInput) -> SkepticAssessment:
    user_prompt = format_skeptic_user(
        topic=input.belief.topic,
        statement=input.belief.statement,
        confidence=input.belief.confidence,
        supporting_block=_format_claim_block(input.supporting_claims),
        contradicting_block=_format_claim_block(input.contradicting_claims),
        entities_block=_format_entities_block(input.in_scope_entities),
        today=datetime.now(UTC).strftime("%Y-%m-%d"),
        n_supporting=len(input.supporting_claims),
        n_contradicting=len(input.contradicting_claims),
    )
    result, _ = llm.complete_with_latency(
        name="challenge_belief",
        system=SKEPTIC_SYSTEM,
        user=user_prompt,
        response_model=SkepticAssessment,
    )
    assert isinstance(result, SkepticAssessment)
    return _filter_to_scope(result, input.in_scope_entities)


def challenge_belief_pure(llm: LLMClient, input: SkepticInput) -> SkepticAssessment:
    """Synchronous pure entry point — used by both the agent and the A2A handler."""
    try:
        return _assess_sync(llm, input)
    except LLMProviderNotReadyError:
        raise
    except LLMResponseError as exc:
        logger.warning(
            "skeptic_parse_failure",
            extra={"belief_id": input.belief.belief_id, "error": str(exc)},
        )
        return _INCONCLUSIVE.model_copy()


def _build_handler(llm: LLMClient) -> Any:
    async def _handle_challenge_belief(payload: dict[str, Any]) -> dict[str, Any]:
        skill_input = ChallengeBeliefSkillInput.model_validate(payload)
        agent_input = SkepticInput(
            belief=BeliefSummary.model_validate(skill_input.belief),
            supporting_claims=[
                HydratedClaim.model_validate(c) for c in skill_input.supporting_claims
            ],
            contradicting_claims=[
                HydratedClaim.model_validate(c) for c in skill_input.contradicting_claims
            ],
            in_scope_entities=[
                InScopeEntity.model_validate(e) for e in skill_input.in_scope_entities
            ],
        )
        assessment = await asyncio.to_thread(challenge_belief_pure, llm, agent_input)
        return ChallengeBeliefSkillOutput(
            verdict=assessment.verdict,
            confidence=assessment.confidence,
            rationale=assessment.rationale,
            suggested_confidence_delta=assessment.suggested_confidence_delta,
            counter_claims=[c.model_dump(mode="json") for c in assessment.counter_claims],
        ).model_dump(mode="json")

    return _handle_challenge_belief


class SkepticAgent(BaseAgent):
    name = "skeptic"

    def __init__(self, llm: LLMClient | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> SkepticAssessment:
        assert isinstance(input, SkepticInput)
        assert self.llm is not None, "SkepticAgent requires an llm client"
        return await asyncio.to_thread(challenge_belief_pure, self.llm, input)

    def to_a2a_server(self, url: str) -> Starlette:
        assert self.llm is not None, "SkepticAgent requires an llm client"
        card = build_agent_card(
            name="Skeptic",
            description=(
                "Challenges existing beliefs by finding evidence problems "
                "and emitting counter-claims."
            ),
            url=url,
            skill_id="challenge_belief",
            skill_name="Challenge Belief",
            skill_description=(
                "Assess a belief against its supporting claims and emit counter-claims "
                "for evidence problems (staleness, contradiction, missing evidence)."
            ),
            skill_tags=["falsification", "skeptic", "beliefs"],
        )
        return build_task_app(
            agent_card=card,
            skill_handlers={"challenge_belief": _build_handler(self.llm)},
            agent_name="skeptic",
        )
