"""POST /api/v1/ask — grounded Q&A over a field's knowledge graph.

Mirrors the briefing router: a read-only endpoint that dispatches an A2A agent
(here ``research_qa``) and returns its synthesized result. Retrieval and
answering happen inside the agent (which reads the field-scoped mesh on the
``mesh_reader`` role); the API just forwards the question + field and validates
the cited answer back out.

Degradation: when the agent can't be reached at all, return a clean
``uncovered`` answer with a caveat (a 200, never a 500) so the wiki can render
the honest "assistant unavailable" state. A genuine timeout or skill error
surfaces as 504/502.
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, HTTPException, Query
from mesh_a2a.client import MeshA2AClient, SkillCallError, TaskTimeoutError
from mesh_models.qa import Answer, Coverage
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ask", tags=["ask"])

_TIMEOUT = float(os.environ.get("MESH_ASK_TIMEOUT", "120"))


class AskRequest(BaseModel):
    question: str


def _agent_urls() -> list[str]:
    raw = os.environ.get("MESH_ASK_AGENT_URLS") or os.environ.get("MESH_RESEARCH_QA_URL")
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]
    return ["http://research-qa:8016"]


def _unavailable() -> Answer:
    return Answer(
        answer_markdown=(
            "The research assistant is currently unavailable, so there is no "
            "grounded answer to give right now. Please try again shortly."
        ),
        citations=[],
        coverage=Coverage.uncovered,
        caveats=["The Q&A agent could not be reached."],
    )


@router.post(
    "",
    response_model=Answer,
    summary="Ask a grounded question about a field",
    description=(
        "Answers a natural-language question using only the requested field's "
        "knowledge graph. The answer is grounded strictly in retrieved mesh "
        "rows with inline citations to beliefs/claims/entities, carries a "
        "coverage signal derived from the evidence, and returns 'uncovered' "
        "when the mesh has no relevant evidence. Read-only; nothing is "
        "persisted."
    ),
)
async def ask(
    body: AskRequest,
    field: str = Query("ai-robotics", description="Field slug to scope the answer to"),
) -> Answer:
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="question must not be empty")

    payload = {"question": question, "field_id": field}
    async with MeshA2AClient() as client:
        discovered = await client.discover(_agent_urls())
        if "research_qa" not in discovered:
            logger.warning("research_qa_agent_unreachable", extra={"field": field})
            return _unavailable()
        try:
            result = await client.call_skill_blocking(
                "research_qa", payload, timeout=_TIMEOUT
            )
        except TaskTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except SkillCallError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Answer.model_validate(result)
