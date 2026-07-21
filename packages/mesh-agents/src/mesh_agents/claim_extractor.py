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
from mesh_llm.prompts import build_claim_extraction_system, format_extraction_user
from mesh_models.field import DEFAULT_FIELD_ID, FieldProfile
from pydantic import BaseModel, Field
from starlette.applications import Starlette

from mesh_agents.arxiv_scout import ScoutedPaper
from mesh_agents.base import BaseAgent
from mesh_agents.memory import build_memory_capture, debug_envelope
from mesh_agents.profiles import load_profile

logger = logging.getLogger(__name__)


class ClaimObject(BaseModel):
    """The structured payload of an extracted claim — the keys belief synthesis
    reads (benchmark/score → SOTA beliefs, capability → capability beliefs,
    compared_to/lab/parent → graph edges).

    Which keys are filled depends on the predicate (see the extraction prompt).
    **Declaring them explicitly is load-bearing.** The field used to be an
    untyped ``dict[str, Any]``; under Anthropic's strict structured output that
    exposes NO properties to the schema, so the model could only ever return
    ``{}`` — every claim persisted with an empty object and belief synthesis
    produced nothing. Naming the keys lets the model fill them.

    Scope: only the keys belief synthesis reads. Fields are typed with empty
    defaults rather than ``X | None`` (each ``| None`` adds an ``anyOf[type,
    null]`` branch), and the set is kept small — Anthropic's parse schema rejects
    overly large/complex models ("schema is too complex"), and the
    reproduces/critiques/speculates predicates aren't synthesized into beliefs,
    so their old object keys (target/outcome/issue/about/prediction) are dropped;
    their evidence lives in the claim's ``raw_excerpt``. Unset keys come back as
    their default and are stripped via ``model_dump(exclude_defaults=True)``, so
    consumers still see a tight dict with only the populated keys."""

    # achieves_score: {"score": <number>, "benchmark": "<name>", "metric": "<opt>"}
    score: float = 0.0
    benchmark: str = ""
    metric: str = ""
    # outperforms: {"compared_to": "<entity>", "on": "<task/benchmark>"}
    compared_to: str = ""
    on: str = ""
    # developed_by: {"lab": "<lab name>"}
    lab: str = ""
    # based_on: {"parent": "<entity it builds on>"}
    parent: str = ""
    # has_capability: {"capability": "<short phrase, no number>"}
    capability: str = ""


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
    # What kind of thing the subject is — a free string drawn from the field's
    # entity-type vocabulary (rule 4b of the extraction prompt). Field-agnostic:
    # ai-robotics uses model/paper/benchmark/…, a hockey field player/team/coach.
    # Drives entity minting; "concept" is the universal fallback.
    subject_type: str = "concept"
    object: ClaimObject = Field(default_factory=ClaimObject)
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
    field_id: str = DEFAULT_FIELD_ID


class ExtractClaimsSkillOutput(BaseModel):
    claims: list[dict[str, Any]]
    entities_referenced: list[str]
    latency_ms: int = 0
    usage: dict[str, int] = Field(default_factory=dict)
    model: str = ""
    # Optional, additive observability envelope (Phase 23a): the memory block +
    # applied heuristic ids + system-prefix hash this run injected. Ignored by
    # every consumer except the coordinator's invocation recorder.
    debug: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Shared extraction logic
# ---------------------------------------------------------------------------


def _extract_sync(
    llm: Any,
    paper: ScoutedPaper,
    memory_block: str = "",
    profile: FieldProfile | None = None,
) -> tuple[list[ExtractedClaim], int, LLMUsage, str]:
    user_prompt = format_extraction_user(title=paper.title, abstract=paper.abstract)
    # Phase 16a/d: fold the extractor's applicable heuristics + recent history
    # into the USER message, before the task content but after the cached system
    # prefix (cache-prefix stability). Phase 17b: the system prefix is built from
    # the active field's profile (per-field-stable; never per-item).
    if memory_block:
        user_prompt = f"{memory_block}\n\n{user_prompt}"
    result, latency_ms, usage = llm.complete_with_usage(
        name="extract_claims",
        system=build_claim_extraction_system(profile),
        user=user_prompt,
        response_model=ClaimExtractionResult,
    )
    # usage.model is the realized model (correct even when a RoutedLLMClient
    # escalated cheap→strong); fall back to the client attribute if unset.
    return result.claims, latency_ms, usage, usage.model or getattr(llm, "model", "")


def extract_claims_with_memory(
    llm: Any,
    paper: ScoutedPaper,
    agent_name: str = "claim_extractor",
    field_id: str = DEFAULT_FIELD_ID,
    conn: Any | None = None,
) -> tuple[list[ExtractedClaim], int, LLMUsage, str, dict[str, Any]]:
    """The full agentic extraction: gather the extractor's applicable heuristics +
    recent history, then extract with that memory folded into the prompt. This is
    the unit both the controller's ``extract-source`` skill and the (orphaned) A2A
    handler call — there is one extraction implementation.

    Heuristics are scoped to this paper's source TYPE (so source-specific how-to
    like "forum scores are self-reported" applies); episodic recall is the
    extractor's broad recent track record (a freshly-scouted source has no prior
    extraction to key on). All scoped to ``field_id``; the system prompt is built
    from that field's profile. ``conn`` (optional) is the connection memory reads
    run on — the skill passes its own; the A2A handler lets it open one.

    Returns the usual ``(claims, latency_ms, usage, model)`` plus the additive
    observability ``debug`` envelope (the memory it injected) for the coordinator
    to record (Phase 23a)."""
    profile = load_profile(field_id)
    memory_block, heuristic_ids = build_memory_capture(
        agent_name, "extract_claims", conn=conn,
        source=paper.source.type.value, field_id=field_id,
    )
    claims, latency_ms, usage, model = _extract_sync(llm, paper, memory_block, profile)
    debug = debug_envelope(
        agent=agent_name,
        memory_block=memory_block,
        applied_heuristic_ids=heuristic_ids,
        system_prefix=build_claim_extraction_system(profile),
    )
    return claims, latency_ms, usage, model, debug


def _build_handler(llm: LLMClient, agent_name: str) -> Any:
    async def _handle_extract_claims(payload: dict[str, Any]) -> dict[str, Any]:
        skill_input = ExtractClaimsSkillInput.model_validate(payload)
        paper = ScoutedPaper.model_validate(skill_input.paper)
        debug: dict[str, Any] | None
        try:
            claims, latency_ms, usage, model, debug = await asyncio.to_thread(
                extract_claims_with_memory, llm, paper, agent_name, skill_input.field_id
            )
        except LLMProviderNotReadyError:
            raise
        except LLMResponseError as exc:
            logger.warning(
                "claim_extraction_parse_failure",
                extra={"arxiv_id": paper.arxiv_id, "error": str(exc)},
            )
            claims, latency_ms, usage, model = [], 0, LLMUsage(), getattr(llm, "model", "")
            debug = None
        entities_referenced = list({c.subject_name for c in claims})
        return ExtractClaimsSkillOutput(
            claims=[c.model_dump(mode="json") for c in claims],
            entities_referenced=entities_referenced,
            latency_ms=latency_ms,
            usage=usage.model_dump(),
            model=model,
            debug=debug,
        ).model_dump(mode="json")

    return _handle_extract_claims


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class ClaimExtractorAgent(BaseAgent):
    """A2A adapter over the shared extraction core (``extract_claims_with_memory``
    / ``_extract_sync``). The controller path no longer uses this class — the
    ``extract-source`` skill calls the core function directly; this remains the
    network entry point for the (orphaned) A2A server in ``apps/agents``."""

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
