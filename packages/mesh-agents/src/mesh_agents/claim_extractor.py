from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from mesh_a2a.card_builder import build_agent_card
from mesh_a2a.task_server import build_task_app
from mesh_llm import (
    LLMClient,
    LLMProviderNotReadyError,
    LLMResponseError,
    LLMUsage,
)
from mesh_llm.prompts import CLAIM_EXTRACTION_SYSTEM, format_extraction_user
from pydantic import BaseModel, Field
from starlette.applications import Starlette

from mesh_agents.arxiv_scout import ScoutedPaper
from mesh_agents.base import BaseAgent
from mesh_agents.memory import build_memory_block

logger = logging.getLogger(__name__)


class ExtractedClaim(BaseModel):
    predicate: Literal[
        # legacy four
        "achieves_score",
        "outperforms",
        "developed_by",
        "evaluated_on",
        # Phase 14a additions (claim_type is derived from these downstream)
        "has_capability",
        "based_on",
        "reproduces",
        "critiques",
        "speculates",
    ]
    subject_name: str
    object: dict[str, Any]
    raw_excerpt: str
    confidence: float = Field(default=0.85, ge=0.0, le=1.0)


class ClaimExtractionResult(BaseModel):
    claims: list[ExtractedClaim]


class ClaimExtractorInput(BaseModel):
    paper: ScoutedPaper


class ClaimExtractorOutput(BaseModel):
    claims: list[ExtractedClaim]
    entities_referenced: list[str]
    latency_ms: int = 0
    usage: LLMUsage = Field(default_factory=LLMUsage)
    model: str = ""


# ---------------------------------------------------------------------------
# Phase 2 A2A skill types
# ---------------------------------------------------------------------------


class ExtractClaimsSkillInput(BaseModel):
    paper: dict[str, Any]  # ScoutedPaper as JSON dict


class ExtractClaimsSkillOutput(BaseModel):
    claims: list[dict[str, Any]]
    entities_referenced: list[str]
    latency_ms: int = 0
    usage: dict[str, int] = Field(default_factory=dict)
    model: str = ""


# ---------------------------------------------------------------------------
# Shared extraction logic
# ---------------------------------------------------------------------------


def _extract_sync(
    llm: Any, paper: ScoutedPaper, memory_block: str = ""
) -> tuple[list[ExtractedClaim], int, LLMUsage, str]:
    user_prompt = format_extraction_user(title=paper.title, abstract=paper.abstract)
    # Phase 16a/d: fold the extractor's applicable heuristics + recent history
    # into the USER message, before the task content but after the cached system
    # prefix (cache-prefix stability).
    if memory_block:
        user_prompt = f"{memory_block}\n\n{user_prompt}"
    result, latency_ms, usage = llm.complete_with_usage(
        name="extract_claims",
        system=CLAIM_EXTRACTION_SYSTEM,
        user=user_prompt,
        response_model=ClaimExtractionResult,
    )
    return result.claims, latency_ms, usage, getattr(llm, "model", "")


def _extract_with_memory(
    llm: Any, paper: ScoutedPaper, agent_name: str
) -> tuple[list[ExtractedClaim], int, LLMUsage, str]:
    """Gather the extractor's applicable heuristics + recent history (off the
    event loop), then extract with that memory folded into the prompt.

    Heuristics are scoped to this paper's source TYPE (so source-specific how-to
    like "forum scores are self-reported" applies); episodic recall is the
    extractor's broad recent track record (a freshly-scouted source has no prior
    extraction to key on)."""
    memory_block = build_memory_block(
        agent_name, "extract_claims", source=paper.source.type.value
    )
    return _extract_sync(llm, paper, memory_block)


def _build_handler(llm: LLMClient, agent_name: str) -> Any:
    async def _handle_extract_claims(payload: dict[str, Any]) -> dict[str, Any]:
        skill_input = ExtractClaimsSkillInput.model_validate(payload)
        paper = ScoutedPaper.model_validate(skill_input.paper)
        try:
            claims, latency_ms, usage, model = await asyncio.to_thread(
                _extract_with_memory, llm, paper, agent_name
            )
        except LLMProviderNotReadyError:
            raise
        except LLMResponseError as exc:
            logger.warning(
                "claim_extraction_parse_failure",
                extra={"arxiv_id": paper.arxiv_id, "error": str(exc)},
            )
            claims, latency_ms, usage, model = [], 0, LLMUsage(), getattr(llm, "model", "")
        entities_referenced = list({c.subject_name for c in claims})
        return ExtractClaimsSkillOutput(
            claims=[c.model_dump(mode="json") for c in claims],
            entities_referenced=entities_referenced,
            latency_ms=latency_ms,
            usage=usage.model_dump(),
            model=model,
        ).model_dump(mode="json")

    return _handle_extract_claims


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class ClaimExtractorAgent(BaseAgent):
    name = "claim_extractor"

    def __init__(self, llm: LLMClient | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> ClaimExtractorOutput:
        assert isinstance(input, ClaimExtractorInput)
        assert self.llm is not None, "ClaimExtractorAgent requires an llm client"

        try:
            claims, latency_ms, usage, model = await asyncio.to_thread(
                _extract_sync, self.llm, input.paper
            )
        except LLMProviderNotReadyError:
            raise
        except LLMResponseError as exc:
            logger.warning(
                "claim_extraction_parse_failure",
                extra={"arxiv_id": input.paper.arxiv_id, "error": str(exc)},
            )
            return ClaimExtractorOutput(claims=[], entities_referenced=[], latency_ms=0)

        entities_referenced = list({c.subject_name for c in claims})
        return ClaimExtractorOutput(
            claims=claims,
            entities_referenced=entities_referenced,
            latency_ms=latency_ms,
            usage=usage,
            model=model,
        )

    def to_a2a_server(self, url: str) -> Starlette:
        assert self.llm is not None, "ClaimExtractorAgent requires an llm client"
        card = build_agent_card(
            name="Claim Extractor",
            description="Extracts structured claims from arXiv paper abstracts using an LLM.",
            url=url,
            skill_id="extract_claims",
            skill_name="Extract Claims",
            skill_description="Extract structured claims (predicates, subjects, objects).",
            skill_tags=["llm", "claims", "extraction"],
        )
        return build_task_app(
            agent_card=card,
            skill_handlers={"extract_claims": _build_handler(self.llm, self.name)},
            agent_name=self.name,
        )
