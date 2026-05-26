"""Personalizer agent — ranks the last 24h of mesh activity against a profile.

The user authors a free-form markdown profile (default location
``~/.config/agent_mesh/profile.md``, override via ``$MESH_PROFILE_PATH``)
describing what they care about. The Personalizer LLM call takes that
profile plus a set of candidate beliefs/revisions/claims and returns a
ranked Briefing object with per-item rationale.

The agent is pure: it never reads the DB. The /briefing API endpoint
gathers candidates and dispatches via A2A, so the orchestrator side
owns all DB access.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, date, datetime
from typing import Any

from mesh_a2a.card_builder import build_agent_card
from mesh_a2a.task_server import build_task_app
from mesh_llm import LLMClient, LLMProviderNotReadyError, LLMResponseError
from mesh_llm.prompts import PERSONALIZER_SYSTEM, format_personalizer_user
from mesh_models.briefing import Briefing, BriefingSection
from pydantic import BaseModel, Field
from starlette.applications import Starlette

from mesh_agents.base import BaseAgent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Skill I/O types
# ---------------------------------------------------------------------------


class BeliefCandidate(BaseModel):
    id: str
    topic: str
    statement: str
    confidence: float
    created_at: datetime | None = None


class RevisionCandidate(BaseModel):
    id: str
    belief_id: str
    belief_topic: str
    previous_statement: str
    new_statement: str
    previous_confidence: float
    new_confidence: float
    revised_by_agent: str
    rationale: str | None = None
    revised_at: datetime | None = None


class ClaimCandidate(BaseModel):
    id: str
    predicate: str
    subject_entity_id: str
    object: dict[str, Any] = Field(default_factory=dict)
    raw_excerpt: str
    confidence: float


class PersonalizeDigestSkillInput(BaseModel):
    profile_text: str
    target_date: date | None = None
    beliefs: list[BeliefCandidate] = Field(default_factory=list)
    revisions: list[RevisionCandidate] = Field(default_factory=list)
    claims: list[ClaimCandidate] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompt-block formatting
# ---------------------------------------------------------------------------


def _format_belief(b: BeliefCandidate) -> str:
    return (
        f"- id={b.id} topic={b.topic!r} confidence={b.confidence:.2f} "
        f"statement={b.statement!r}"
    )


def _format_revision(r: RevisionCandidate) -> str:
    delta = f"{r.previous_confidence:.2f}->{r.new_confidence:.2f}"
    return (
        f"- id={r.id} belief_id={r.belief_id} topic={r.belief_topic!r} "
        f"by={r.revised_by_agent} confidence_change={delta} "
        f"new_statement={r.new_statement!r} "
        f"rationale={r.rationale!r}"
    )


def _format_claim(c: ClaimCandidate) -> str:
    return (
        f"- id={c.id} predicate={c.predicate} subject_entity_id={c.subject_entity_id} "
        f"object={json.dumps(c.object, default=str)} confidence={c.confidence:.2f} "
        f"excerpt={c.raw_excerpt!r}"
    )


def _personalize_sync(llm: LLMClient, payload: PersonalizeDigestSkillInput) -> Briefing:
    today = (payload.target_date or datetime.now(UTC).date()).isoformat()
    user_prompt = format_personalizer_user(
        profile_text=payload.profile_text,
        beliefs_block="\n".join(_format_belief(b) for b in payload.beliefs),
        revisions_block="\n".join(_format_revision(r) for r in payload.revisions),
        claims_block="\n".join(_format_claim(c) for c in payload.claims),
        today=today,
        n_beliefs=len(payload.beliefs),
        n_revisions=len(payload.revisions),
        n_claims=len(payload.claims),
    )
    result, _ = llm.complete_with_latency(
        name="personalize_digest",
        system=PERSONALIZER_SYSTEM,
        user=user_prompt,
        response_model=Briefing,
    )
    assert isinstance(result, Briefing)
    return _filter_to_candidates(result, payload)


def _filter_to_candidates(briefing: Briefing, payload: PersonalizeDigestSkillInput) -> Briefing:
    """Drop any item whose id wasn't in the candidate set.

    Defensive — the prompt says "never invent ids" but the LLM may still
    drift. Filtering here keeps the wiki from rendering broken links.
    """
    allowed: dict[str, set[str]] = {
        "belief": {b.id for b in payload.beliefs},
        "revision": {r.id for r in payload.revisions},
        "claim": {c.id for c in payload.claims},
    }
    cleaned_sections: list[BriefingSection] = []
    for section in briefing.sections:
        kept = [
            item
            for item in section.items
            if item.item_id in allowed.get(item.item_type, set())
        ]
        if len(kept) < len(section.items):
            logger.warning(
                "personalizer_dropped_invalid_ids",
                extra={
                    "section": section.name,
                    "dropped": len(section.items) - len(kept),
                },
            )
        cleaned_sections.append(section.model_copy(update={"items": kept}))
    return briefing.model_copy(update={"sections": cleaned_sections})


_EMPTY_BRIEFING = Briefing(date=datetime.now(UTC).date(), sections=[])


def _build_handler(llm: LLMClient) -> Any:
    async def _handle_personalize_digest(payload: dict[str, Any]) -> dict[str, Any]:
        skill_input = PersonalizeDigestSkillInput.model_validate(payload)
        try:
            briefing = await asyncio.to_thread(_personalize_sync, llm, skill_input)
        except LLMProviderNotReadyError:
            raise
        except LLMResponseError as exc:
            logger.warning("personalizer_parse_failure", extra={"error": str(exc)})
            briefing = _EMPTY_BRIEFING.model_copy(
                update={"date": skill_input.target_date or datetime.now(UTC).date()}
            )
        return briefing.model_dump(mode="json")

    return _handle_personalize_digest


class PersonalizerAgent(BaseAgent):
    name = "personalizer"

    def __init__(self, llm: LLMClient | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> Briefing:  # pragma: no cover
        raise NotImplementedError("PersonalizerAgent uses the A2A skill path only")

    def to_a2a_server(self, url: str) -> Starlette:
        assert self.llm is not None, "PersonalizerAgent requires an llm client"
        card = build_agent_card(
            name="Personalizer",
            description=(
                "Filters the last 24h of mesh activity against a user-authored "
                "markdown profile and returns a ranked daily briefing."
            ),
            url=url,
            skill_id="personalize_digest",
            skill_name="Personalize Digest",
            skill_description=(
                "Rank candidate beliefs/revisions/claims by relevance to a "
                "markdown profile and return per-item rationale."
            ),
            skill_tags=["personalization", "briefing", "ranking"],
        )
        return build_task_app(
            agent_card=card,
            skill_handlers={"personalize_digest": _build_handler(self.llm)},
            agent_name="personalizer",
        )
