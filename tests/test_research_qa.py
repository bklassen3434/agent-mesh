"""Phase 21b — grounded answer agent.

Covers the grounding contract: a question with supporting evidence yields a
cited answer whose every citation id is in the retrieved pack; hallucinated
citation ids are dropped; an out-of-corpus question short-circuits to
``uncovered`` without an LLM call; coverage is derived from the evidence's own
signals; a parse failure degrades to ``uncovered``.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from mesh_agents.research_qa import (
    ResearchQAAgent,
    ResearchQAInput,
    _format_context,
    _has_strong_evidence,
    _validate_citations,
    answer_question_pure,
)
from mesh_db.beliefs import create_belief
from mesh_db.claims import create_claim
from mesh_db.connection import MeshConnection
from mesh_db.entities import create_entity
from mesh_db.search import ContextPack, ScoredBelief
from mesh_db.sources import create_source
from mesh_llm import LLMProviderNotReadyError, LLMResponseError, LLMUsage
from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.entity import Entity, EntityType
from mesh_models.field import DEFAULT_FIELD_ID
from mesh_models.qa import Answer, Citation, Coverage
from mesh_models.source import Source, SourceType

# ── mock LLM ─────────────────────────────────────────────────────────────────


class MockLLM:
    model = "mock-model"

    def __init__(self, answer: Answer | None = None, raise_exc: Exception | None = None):
        self._answer = answer
        self._raise = raise_exc
        self.calls = 0

    def complete_with_latency(
        self,
        name: str,
        system: str,
        user: str,
        response_model: type[Any] | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[Any, int]:
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        assert response_model is Answer
        return self._answer, 5

    def complete_with_usage(
        self,
        name: str,
        system: str,
        user: str,
        response_model: type[Any] | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[Any, int, LLMUsage]:
        result, latency = self.complete_with_latency(
            name, system, user, response_model, options
        )
        return result, latency, LLMUsage()

    def health_check(self) -> None:  # pragma: no cover
        return None


# ── seed helpers ─────────────────────────────────────────────────────────────


def _src(conn: MeshConnection, url: str, type_: SourceType = SourceType.arxiv) -> Source:
    s = Source(type=type_, url=url, published_at=datetime.now(UTC), raw_content_hash=url)
    create_source(conn, s, field_id=DEFAULT_FIELD_ID)
    return s


def _ent(conn: MeshConnection, name: str) -> Entity:
    e = Entity(canonical_name=name, type=EntityType.model)
    create_entity(conn, e, field_id=DEFAULT_FIELD_ID)
    return e


def _claim(conn: MeshConnection, entity_id: str, source_id: str, excerpt: str) -> Claim:
    c = Claim(
        predicate="has_capability",
        subject_entity_id=entity_id,
        object={"capability": "x"},
        source_id=source_id,
        extracted_by_agent="claim_extractor",
        raw_excerpt=excerpt,
    )
    create_claim(conn, c, field_id=DEFAULT_FIELD_ID)
    return c


def _belief(conn: MeshConnection, topic: str, statement: str, supporting: list[str]) -> Belief:
    b = Belief(
        topic=topic,
        statement=statement,
        supporting_claim_ids=supporting,
        last_revised_at=datetime.now(UTC),
    )
    create_belief(conn, b, field_id=DEFAULT_FIELD_ID)
    return b


# ── pure helper unit tests (stub pack) ───────────────────────────────────────


def _stub_pack() -> ContextPack:
    b = Belief(topic="t", statement="s", last_revised_at=datetime.now(UTC))
    e = Entity(canonical_name="Atlas", type=EntityType.model)
    c = Claim(
        predicate="has_capability",
        subject_entity_id=e.id,
        object={},
        source_id="s1",
        extracted_by_agent="x",
        raw_excerpt="ex",
    )
    return ContextPack(
        question="q",
        field_id=DEFAULT_FIELD_ID,
        beliefs=[ScoredBelief(belief=b, signals={"source_type_diversity": 0}, rank=0.1)],
        claims=[c],
        entities=[e],
    )


def test_validate_citations_drops_hallucinated() -> None:
    pack = _stub_pack()
    real_belief = pack.beliefs[0].belief.id
    answer = Answer(
        answer_markdown="...",
        citations=[
            Citation(kind="belief", id=real_belief, quote="ok"),
            Citation(kind="claim", id="does-not-exist", quote="bad"),
            Citation(kind="entity", id="also-fake", quote="bad"),
        ],
    )
    kept = _validate_citations(answer, pack)
    assert [c.id for c in kept] == [real_belief]


def test_has_strong_evidence_from_signals() -> None:
    pack = _stub_pack()
    assert not _has_strong_evidence(pack)
    pack.beliefs[0].signals["source_type_diversity"] = 2
    assert _has_strong_evidence(pack)


def test_format_context_tags_ids() -> None:
    pack = _stub_pack()
    block = _format_context(pack)
    assert f"[belief:{pack.beliefs[0].belief.id}]" in block
    assert "[claim:" in block and "[entity:" in block


# ── end-to-end agent path (testcontainer DB + mock LLM) ──────────────────────


def test_grounded_answer_keeps_valid_citations(tmp_db: MeshConnection) -> None:
    e = _ent(tmp_db, "Atlas")
    s = _src(tmp_db, "http://a")
    c = _claim(tmp_db, e.id, s.id, "Atlas performs dynamic bipedal parkour.")
    b = _belief(tmp_db, "locomotion", "Atlas leads bipedal locomotion.", [c.id])

    llm = MockLLM(
        answer=Answer(
            answer_markdown=f"Atlas leads locomotion [belief:{b.id}].",
            citations=[
                Citation(kind="belief", id=b.id, quote="leads"),
                Citation(kind="claim", id="hallucinated", quote="nope"),
            ],
            coverage=Coverage.well_supported,
        )
    )
    answer = answer_question_pure(
        llm, "Atlas bipedal locomotion", field_id=DEFAULT_FIELD_ID, conn=tmp_db
    )
    assert llm.calls == 1
    ids = {c.id for c in answer.citations}
    assert b.id in ids
    assert "hallucinated" not in ids


def test_out_of_corpus_is_uncovered_without_llm(tmp_db: MeshConnection) -> None:
    _belief(tmp_db, "vision", "Diffusion models make images.", [])
    llm = MockLLM(answer=Answer(answer_markdown="should not be used"))
    answer = answer_question_pure(
        llm, "lattice quantum chromodynamics gauge", field_id=DEFAULT_FIELD_ID, conn=tmp_db
    )
    assert llm.calls == 0
    assert answer.coverage is Coverage.uncovered
    assert answer.citations == []


def test_parse_failure_degrades_to_uncovered(tmp_db: MeshConnection) -> None:
    e = _ent(tmp_db, "Atlas")
    s = _src(tmp_db, "http://a")
    c = _claim(tmp_db, e.id, s.id, "Atlas performs bipedal parkour.")
    _belief(tmp_db, "locomotion", "Atlas leads bipedal locomotion.", [c.id])

    llm = MockLLM(raise_exc=LLMResponseError("bad json"))
    answer = answer_question_pure(
        llm, "Atlas locomotion", field_id=DEFAULT_FIELD_ID, conn=tmp_db
    )
    assert answer.coverage is Coverage.uncovered


def test_provider_not_ready_propagates(tmp_db: MeshConnection) -> None:
    e = _ent(tmp_db, "Atlas")
    s = _src(tmp_db, "http://a")
    c = _claim(tmp_db, e.id, s.id, "Atlas performs bipedal parkour.")
    _belief(tmp_db, "locomotion", "Atlas leads bipedal locomotion.", [c.id])

    llm = MockLLM(raise_exc=LLMProviderNotReadyError("offline"))
    with pytest.raises(LLMProviderNotReadyError):
        answer_question_pure(llm, "Atlas locomotion", field_id=DEFAULT_FIELD_ID, conn=tmp_db)


def test_coverage_well_supported_from_source_diversity(tmp_db: MeshConnection) -> None:
    e = _ent(tmp_db, "Atlas")
    s1 = _src(tmp_db, "http://a", SourceType.arxiv)
    s2 = _src(tmp_db, "http://b", SourceType.github)
    c1 = _claim(tmp_db, e.id, s1.id, "Atlas performs bipedal parkour reliably.")
    c2 = _claim(tmp_db, e.id, s2.id, "Atlas performs bipedal parkour in the field.")
    b = _belief(tmp_db, "locomotion", "Atlas leads bipedal locomotion.", [c1.id, c2.id])

    llm = MockLLM(
        answer=Answer(
            answer_markdown=f"Atlas leads [belief:{b.id}].",
            citations=[Citation(kind="belief", id=b.id)],
            coverage=Coverage.thin,  # model says thin; signals upgrade it
        )
    )
    answer = answer_question_pure(
        llm, "Atlas bipedal locomotion", field_id=DEFAULT_FIELD_ID, conn=tmp_db
    )
    assert answer.coverage is Coverage.well_supported


def test_model_uncovered_verdict_is_respected(tmp_db: MeshConnection) -> None:
    e = _ent(tmp_db, "Atlas")
    s = _src(tmp_db, "http://a")
    c = _claim(tmp_db, e.id, s.id, "Atlas performs bipedal parkour.")
    b = _belief(tmp_db, "locomotion", "Atlas leads bipedal locomotion.", [c.id])

    # Rows were retrieved, but the model judged them irrelevant → uncovered.
    llm = MockLLM(
        answer=Answer(
            answer_markdown="The mesh has no evidence on this.",
            citations=[Citation(kind="belief", id=b.id)],
            coverage=Coverage.uncovered,
        )
    )
    answer = answer_question_pure(
        llm, "Atlas bipedal locomotion", field_id=DEFAULT_FIELD_ID, conn=tmp_db
    )
    assert answer.coverage is Coverage.uncovered


def test_agent_run_in_process(tmp_db: MeshConnection) -> None:
    e = _ent(tmp_db, "Atlas")
    s = _src(tmp_db, "http://a")
    c = _claim(tmp_db, e.id, s.id, "Atlas performs bipedal parkour.")
    b = _belief(tmp_db, "locomotion", "Atlas leads bipedal locomotion.", [c.id])

    llm = MockLLM(
        answer=Answer(
            answer_markdown=f"Atlas [belief:{b.id}].",
            citations=[Citation(kind="belief", id=b.id)],
            coverage=Coverage.well_supported,
        )
    )
    agent = ResearchQAAgent(llm=llm, db_conn=tmp_db)
    answer = asyncio.run(
        agent.run(ResearchQAInput(question="Atlas locomotion", field_id=DEFAULT_FIELD_ID))
    )
    assert isinstance(answer, Answer)
    assert any(cit.id == b.id for cit in answer.citations)
