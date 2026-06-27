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

from fastapi import APIRouter, Header, HTTPException, Query
from mesh_a2a.client import MeshA2AClient, SkillCallError, TaskTimeoutError
from mesh_db.beta_quota import consume_quota, daily_limit, quota_used
from mesh_db.connection import get_connection
from mesh_models.qa import Answer, Coverage
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ask", tags=["ask"])

_TIMEOUT = float(os.environ.get("MESH_ASK_TIMEOUT", "120"))


class AskRequest(BaseModel):
    question: str


class QuotaStatus(BaseModel):
    """A beta browser's remaining daily chatbot questions."""

    limit: int
    used: int
    remaining: int


def _quota_for(beta_id: str) -> QuotaStatus:
    limit = daily_limit()
    conn = get_connection(read_only=True)
    try:
        used = quota_used(conn, beta_id)
    finally:
        conn.close()
    return QuotaStatus(limit=limit, used=used, remaining=max(0, limit - used))


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
    x_mesh_role: str | None = Header(default=None),
    x_mesh_beta_id: str | None = Header(default=None),
) -> Answer:
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="question must not be empty")

    # Quota: admins are unlimited; an anonymous beta browser (identified by the
    # wiki via the X-Mesh-Beta-Id header) gets daily_limit() questions/day. We
    # check before answering but only consume after a real answer, so an
    # unavailable agent or error never burns a question.
    role = (x_mesh_role or "beta").strip().lower()
    beta_id = (x_mesh_beta_id or "").strip()
    enforce = role != "admin" and bool(beta_id)
    if enforce:
        conn = get_connection(read_only=True)
        try:
            remaining = max(0, daily_limit() - quota_used(conn, beta_id))
        finally:
            conn.close()
        if remaining <= 0:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Daily question limit reached ({daily_limit()}/day). "
                    "Sign in as admin for unlimited questions, or try again tomorrow."
                ),
            )

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

    if enforce:
        conn = get_connection(read_only=False)
        try:
            consume_quota(conn, beta_id)
        finally:
            conn.close()
    return Answer.model_validate(result)


@router.get(
    "/quota",
    response_model=QuotaStatus,
    summary="Remaining daily chatbot quota for a beta browser",
    description=(
        "How many grounded questions the identified beta browser has left today. "
        "Admins are unlimited and need not call this. Returns the full limit when "
        "no beta id is supplied."
    ),
)
def ask_quota(x_mesh_beta_id: str | None = Header(default=None)) -> QuotaStatus:
    beta_id = (x_mesh_beta_id or "").strip()
    if not beta_id:
        limit = daily_limit()
        return QuotaStatus(limit=limit, used=0, remaining=limit)
    return _quota_for(beta_id)
