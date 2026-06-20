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
from mesh_llm import LLMClient, LLMProviderNotReadyError, LLMResponseError, LLMUsage
from mesh_llm.prompts import build_skeptic_system, format_skeptic_user
from mesh_models.claim import FailureMode
from mesh_models.field import DEFAULT_FIELD_ID, FieldProfile
from pydantic import BaseModel, Field
from starlette.applications import Starlette

from mesh_agents.base import BaseAgent
from mesh_agents.memory import build_memory_block
from mesh_agents.profiles import load_profile
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
    source_reliability: float | None = None
    extracted_at: datetime | None = None
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
    field_id: str = DEFAULT_FIELD_ID


class ChallengeBeliefSkillOutput(BaseModel):
    verdict: Verdict
    confidence: float
    rationale: str
    suggested_confidence_delta: float
    counter_claims: list[dict[str, Any]]
    usage: dict[str, int] = Field(default_factory=dict)
    model: str = ""


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
        extracted = c.extracted_at.isoformat() if c.extracted_at else "unknown"
        reliability = (
            f"{c.source_reliability:.2f}" if c.source_reliability is not None else "unknown"
        )
        lines.append(
            f"- [{c.claim_id}] predicate={c.predicate} subject_entity_id={c.subject_entity_id} "
            f"object={json.dumps(c.object, default=str)} status={c.status} "
            f"extracted_at={extracted} source_published_at={published} "
            f"source_url={c.source_url or 'unknown'} source_reliability={reliability} "
            f"confidence={c.confidence:.2f} excerpt={c.raw_excerpt!r}"
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


def build_skeptic_prompt(
    input: SkepticInput, memory_block: str = "", profile: FieldProfile | None = None
) -> tuple[str, str]:
    """Return (system, user) for a skeptic assessment.

    Shared by the synchronous agent path and the sweep's Batch-API path (which
    submits requests directly rather than calling this agent over A2A), so both
    reason over identical prompts. ``memory_block`` (Phase 16a/d — the skeptic's
    applicable heuristics + recent challenge history) is prepended to the USER
    message, after the cached system prefix, so the prompt cache prefix stays
    stable."""
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
    if memory_block:
        user_prompt = f"{memory_block}\n\n{user_prompt}"
    return build_skeptic_system(profile), user_prompt


def filter_to_scope(
    assessment: SkepticAssessment, entities: list[InScopeEntity]
) -> SkepticAssessment:
    """Public wrapper: drop counter-claims referencing out-of-scope entities.
    Applied to both sync and batch assessments."""
    return _filter_to_scope(assessment, entities)


def _assess_sync(
    llm: LLMClient,
    input: SkepticInput,
    memory_block: str = "",
    profile: FieldProfile | None = None,
) -> tuple[SkepticAssessment, LLMUsage, str]:
    system, user_prompt = build_skeptic_prompt(input, memory_block, profile)
    result, _, usage = llm.complete_with_usage(
        name="challenge_belief",
        system=system,
        user=user_prompt,
        response_model=SkepticAssessment,
    )
    assert isinstance(result, SkepticAssessment)
    # usage.model is the realized model (correct under cheap→strong routing
    # escalation); fall back to the client attribute if unset.
    model = usage.model or getattr(llm, "model", "")
    return _filter_to_scope(result, input.in_scope_entities), usage, model


def challenge_belief_pure(
    llm: LLMClient,
    input: SkepticInput,
    memory_block: str = "",
    profile: FieldProfile | None = None,
) -> tuple[SkepticAssessment, LLMUsage, str]:
    """Synchronous pure entry point — used by both the agent and the A2A handler.

    Returns ``(assessment, usage, model)``; the agent path discards usage, the
    A2A handler threads it back to the coordinator for the cost ledger.
    ``memory_block`` carries the skeptic's recent history (Phase 16a); ``profile``
    drives the (per-field-stable) system prompt (Phase 17b).
    """
    try:
        return _assess_sync(llm, input, memory_block, profile)
    except LLMProviderNotReadyError:
        raise
    except LLMResponseError as exc:
        logger.warning(
            "skeptic_parse_failure",
            extra={"belief_id": input.belief.belief_id, "error": str(exc)},
        )
        return _INCONCLUSIVE.model_copy(), LLMUsage(), getattr(llm, "model", "")


def challenge_belief_with_memory(
    llm: LLMClient,
    agent_input: SkepticInput,
    agent_name: str = "skeptic",
    field_id: str = DEFAULT_FIELD_ID,
    conn: Any | None = None,
) -> tuple[SkepticAssessment, LLMUsage, str]:
    """The full agentic challenge: gather the skeptic's applicable heuristics +
    challenge history on this belief's topic, then assess with that memory folded
    into the prompt. The unit both the controller's ``challenge-belief`` skill and
    the (orphaned) A2A handler call. Scoped to ``field_id``; the system prompt is
    built from that field's profile. ``conn`` (optional) is the connection memory
    reads run on — the skill passes its own; the A2A handler lets it open one."""
    profile = load_profile(field_id)
    memory_block = build_memory_block(
        agent_name, "challenge_belief", conn=conn,
        topic=agent_input.belief.topic, field_id=field_id,
    )
    return challenge_belief_pure(llm, agent_input, memory_block, profile)


def _build_handler(llm: LLMClient, agent_name: str) -> Any:
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
        assessment, usage, model = await asyncio.to_thread(
            challenge_belief_with_memory, llm, agent_input, agent_name, skill_input.field_id
        )
        return ChallengeBeliefSkillOutput(
            verdict=assessment.verdict,
            confidence=assessment.confidence,
            rationale=assessment.rationale,
            suggested_confidence_delta=assessment.suggested_confidence_delta,
            counter_claims=[c.model_dump(mode="json") for c in assessment.counter_claims],
            usage=usage.model_dump(),
            model=model,
        ).model_dump(mode="json")

    return _handle_challenge_belief


class SkepticAgent(BaseAgent):
    """A2A adapter over the shared challenge core (``challenge_belief_pure`` /
    ``challenge_belief_with_memory``). The controller path no longer uses this
    class — the ``challenge-belief`` skill calls the core directly; this remains
    the network entry point for the (orphaned) A2A server in ``apps/agents``."""

    name = "skeptic"

    def __init__(self, llm: LLMClient | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> SkepticAssessment:
        assert isinstance(input, SkepticInput)
        assert self.llm is not None, "SkepticAgent requires an llm client"
        assessment, _, _ = await asyncio.to_thread(
            challenge_belief_pure, self.llm, input
        )
        return assessment

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
            skill_handlers={"challenge_belief": _build_handler(self.llm, self.name)},
            agent_name=self.name,
        )
