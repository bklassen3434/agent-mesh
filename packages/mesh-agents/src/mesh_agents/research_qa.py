"""ResearchQA agent — grounded Q&A over a field's knowledge graph (Phase 21b).

Given a natural-language question scoped to a field, the agent retrieves the
relevant beliefs/claims/entities/relationships from *that field's* graph
(``mesh_db.gather_context``) and asks an LLM to synthesize an answer grounded
strictly in the retrieved rows, with inline citations.

Grounding and citation are the whole game:
  * the context pack is assembled by deterministic, field-scoped queries;
  * the system prompt forbids outside knowledge and uncited assertions;
  * citations the LLM emits are validated against the pack — hallucinated ids
    are dropped, never rendered;
  * coverage is derived from the retrieved *evidence's own signals*, not a
    number the model invented;
  * an out-of-corpus question short-circuits to ``uncovered`` with a templated
    answer and **no LLM call** (cheap + un-hallucinatable).

The agent reads the DB read-only (the ``mesh_reader`` role in deployment) and
writes nothing.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mesh_a2a.card_builder import build_agent_card
from mesh_a2a.task_server import build_task_app
from mesh_db import ContextPack, gather_context
from mesh_db.connection import MeshConnection, get_connection
from mesh_llm import LLMClient, LLMProviderNotReadyError, LLMResponseError
from mesh_llm.prompts import build_research_qa_system, format_research_qa_user
from mesh_models.field import DEFAULT_FIELD_ID
from mesh_models.qa import Answer, Citation, Coverage
from pydantic import BaseModel
from starlette.applications import Starlette

from mesh_agents.base import BaseAgent
from mesh_agents.profiles import load_profile

logger = logging.getLogger(__name__)


# ── Skill I/O types ──────────────────────────────────────────────────────────


class ResearchQAInput(BaseModel):
    question: str
    field_id: str = DEFAULT_FIELD_ID


# ── context formatting ───────────────────────────────────────────────────────


def _format_context(pack: ContextPack) -> str:
    """Render a citation-id-tagged, LLM-readable block from a context pack."""
    names = {e.id: e.canonical_name for e in pack.entities}
    lines: list[str] = []

    if pack.beliefs:
        lines.append("BELIEFS:")
        for sb in pack.beliefs:
            b, sig = sb.belief, sb.signals
            lines.append(
                f"[belief:{b.id}] (confidence {b.confidence:.2f}; "
                f"source-types {sig.get('source_type_diversity', 0)}, "
                f"reproductions {sig.get('reproduction_count', 0)}, "
                f"skeptic-attacks {sig.get('skeptic_counter_claim_count', 0)})\n"
                f"    topic: {b.topic}\n    {b.statement}"
            )
    if pack.claims:
        lines.append("\nCLAIMS:")
        for c in pack.claims:
            subj = names.get(c.subject_entity_id, c.subject_entity_id)
            lines.append(
                f"[claim:{c.id}] {subj} — {c.predicate} "
                f"{json.dumps(c.object, default=str)}\n"
                f'    "{c.raw_excerpt}"'
            )
    if pack.entities:
        lines.append("\nENTITIES:")
        for e in pack.entities:
            alias = f" (aka {', '.join(e.aliases)})" if e.aliases else ""
            lines.append(f"[entity:{e.id}] {e.canonical_name}{alias} — {e.type.value}")
    if pack.relationships:
        lines.append("\nRELATIONSHIPS (cite the underlying claim/entity, not the edge):")
        for r in pack.relationships:
            frm = names.get(r.from_entity_id, r.from_entity_id)
            to = names.get(r.to_entity_id, r.to_entity_id)
            lines.append(f"    {frm} --{r.type}--> {to} ({len(r.evidence_claim_ids)} claims)")
    return "\n".join(lines)


_UNCOVERED_TEXT = (
    "The mesh has no evidence on this question. Nothing in the knowledge base "
    "addresses it, so there is no grounded answer to give."
)


def _uncovered_answer() -> Answer:
    return Answer(
        answer_markdown=_UNCOVERED_TEXT,
        citations=[],
        coverage=Coverage.uncovered,
        caveats=[],
    )


def _has_strong_evidence(pack: ContextPack) -> bool:
    """Evidence-derived strength: a reproduced / source-diverse belief, or a
    healthy spread of corroborating claims. Mirrors the mesh's own signals so
    coverage reflects the corpus, not the model."""
    strong_belief = any(
        sb.signals.get("source_type_diversity", 0) >= 2
        or sb.signals.get("reproduction_count", 0) >= 1
        for sb in pack.beliefs
    )
    return bool((strong_belief and pack.beliefs) or len(pack.claims) >= 3)


def _validate_citations(answer: Answer, pack: ContextPack) -> list[Citation]:
    """Drop any citation whose id was not in the retrieved context pack."""
    allowed = pack.citation_index()
    kept: list[Citation] = []
    dropped = 0
    for cit in answer.citations:
        if cit.id in allowed.get(cit.kind, set()):
            kept.append(cit)
        else:
            dropped += 1
    if dropped:
        logger.warning(
            "research_qa_dropped_hallucinated_citations",
            extra={"dropped": dropped, "field_id": pack.field_id},
        )
    return kept


def answer_question_pure(
    llm: LLMClient,
    question: str,
    *,
    field_id: str,
    conn: MeshConnection,
) -> Answer:
    """Synchronous core shared by the agent and the A2A handler.

    Never raises on a bad LLM parse — degrades to ``uncovered``. Provider-not-
    ready errors propagate (an unconfigured provider should be loud).
    """
    pack = gather_context(conn, question, field_id=field_id)
    if pack.is_empty():
        return _uncovered_answer()

    try:
        result, _ = llm.complete_with_latency(
            name="research_qa",
            system=build_research_qa_system(load_profile(field_id)),
            user=format_research_qa_user(question, _format_context(pack)),
            response_model=Answer,
        )
    except LLMProviderNotReadyError:
        raise
    except LLMResponseError as exc:
        logger.warning("research_qa_parse_failure", extra={"error": str(exc)})
        return _uncovered_answer()
    assert isinstance(result, Answer)

    citations = _validate_citations(result, pack)
    # Coverage is the mesh's, not the model's: respect a model "uncovered"
    # verdict (it judged the context irrelevant) or the absence of any valid
    # citation, otherwise grade strength from the evidence's own signals.
    if result.coverage is Coverage.uncovered or not citations:
        coverage = Coverage.uncovered
    else:
        coverage = (
            Coverage.well_supported if _has_strong_evidence(pack) else Coverage.thin
        )
    return result.model_copy(update={"citations": citations, "coverage": coverage})


# ── A2A handler + agent ──────────────────────────────────────────────────────


def _build_handler(llm: LLMClient) -> Any:
    async def _handle_research_qa(payload: dict[str, Any]) -> dict[str, Any]:
        skill_input = ResearchQAInput.model_validate(payload)
        conn = get_connection(read_only=True)
        try:
            answer = await asyncio.to_thread(
                answer_question_pure,
                llm,
                skill_input.question,
                field_id=skill_input.field_id,
                conn=conn,
            )
        finally:
            conn.close()
        return answer.model_dump(mode="json")

    return _handle_research_qa


class ResearchQAAgent(BaseAgent):
    name = "research_qa"

    def __init__(self, llm: LLMClient | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> Answer:
        """In-process answer path (used by the CLI). Opens its own reader
        connection when one wasn't injected."""
        assert isinstance(input, ResearchQAInput)
        assert self.llm is not None, "ResearchQAAgent requires an llm client"
        conn = self.db_conn or get_connection(read_only=True)
        owned = self.db_conn is None
        try:
            return await asyncio.to_thread(
                answer_question_pure,
                self.llm,
                input.question,
                field_id=input.field_id,
                conn=conn,
            )
        finally:
            if owned:
                conn.close()

    def to_a2a_server(self, url: str) -> Starlette:
        assert self.llm is not None, "ResearchQAAgent requires an llm client"
        card = build_agent_card(
            name="Research QA",
            description=(
                "Answers natural-language questions about a field using only "
                "that field's knowledge graph, with inline citations."
            ),
            url=url,
            skill_id="research_qa",
            skill_name="Research QA",
            skill_description=(
                "Retrieve field-scoped beliefs/claims/entities for a question "
                "and synthesize a grounded, cited answer with a coverage signal."
            ),
            skill_tags=["qa", "retrieval", "grounded", "citations"],
        )
        return build_task_app(
            agent_card=card,
            skill_handlers={"research_qa": _build_handler(self.llm)},
            agent_name=self.name,
        )
