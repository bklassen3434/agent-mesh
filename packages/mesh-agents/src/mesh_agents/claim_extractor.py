from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from a2a.helpers.proto_helpers import new_data_artifact, new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import TaskArtifactUpdateEvent, TaskState, TaskStatus, TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict
from mesh_a2a.card_builder import build_agent_card
from mesh_llm import (
    LLMClient,
    LLMProviderNotReadyError,
    LLMResponseError,
)
from mesh_llm.prompts import CLAIM_EXTRACTION_SYSTEM, format_extraction_user
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.routing import Route

from mesh_agents.arxiv_scout import ScoutedPaper
from mesh_agents.base import BaseAgent

logger = logging.getLogger(__name__)


class ExtractedClaim(BaseModel):
    predicate: Literal["achieves_score", "outperforms", "developed_by", "evaluated_on"]
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


# ---------------------------------------------------------------------------
# Phase 2 A2A skill types
# ---------------------------------------------------------------------------


class ExtractClaimsSkillInput(BaseModel):
    paper: dict[str, Any]  # ScoutedPaper as JSON dict


class ExtractClaimsSkillOutput(BaseModel):
    claims: list[dict[str, Any]]
    entities_referenced: list[str]
    latency_ms: int = 0


# ---------------------------------------------------------------------------
# Shared extraction logic
# ---------------------------------------------------------------------------


def _extract_sync(llm: Any, paper: ScoutedPaper) -> tuple[list[ExtractedClaim], int]:
    user_prompt = format_extraction_user(title=paper.title, abstract=paper.abstract)
    result, latency_ms = llm.complete_with_latency(
        name="extract_claims",
        system=CLAIM_EXTRACTION_SYSTEM,
        user=user_prompt,
        response_model=ClaimExtractionResult,
    )
    return result.claims, latency_ms


# ---------------------------------------------------------------------------
# A2A executor
# ---------------------------------------------------------------------------


class _ClaimExtractorExecutor(AgentExecutor):
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        assert context.message is not None
        task = new_task_from_user_message(context.message)
        await event_queue.enqueue_event(task)

        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
            )
        )

        raw: dict[str, Any] = {}
        for part in context.message.parts:
            if part.HasField("data"):
                raw = dict(MessageToDict(part.data))
                break
        skill_input = ExtractClaimsSkillInput.model_validate(raw)
        paper = ScoutedPaper.model_validate(skill_input.paper)

        try:
            claims, latency_ms = await asyncio.to_thread(_extract_sync, self._llm, paper)
        except LLMProviderNotReadyError:
            raise
        except LLMResponseError as exc:
            logger.warning(
                "claim_extraction_parse_failure",
                extra={"arxiv_id": paper.arxiv_id, "error": str(exc)},
            )
            claims, latency_ms = [], 0

        entities_referenced = list({c.subject_name for c in claims})
        output = ExtractClaimsSkillOutput(
            claims=[c.model_dump(mode="json") for c in claims],
            entities_referenced=entities_referenced,
            latency_ms=latency_ms,
        )

        await event_queue.enqueue_event(
            TaskArtifactUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                artifact=new_data_artifact("result", output.model_dump(mode="json")),
            )
        )
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("cancel not supported")


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
            claims, latency_ms = await asyncio.to_thread(_extract_sync, self.llm, input.paper)
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
        handler = DefaultRequestHandler(
            agent_executor=_ClaimExtractorExecutor(self.llm),
            task_store=InMemoryTaskStore(),
            agent_card=card,
        )
        routes: list[Route] = []
        routes.extend(create_agent_card_routes(card))
        routes.extend(create_jsonrpc_routes(handler, "/"))
        return Starlette(routes=routes)
