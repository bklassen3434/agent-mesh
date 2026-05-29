from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mesh_agents.skeptic import (
    HydratedClaim,
    InScopeEntity,
    SkepticAgent,
    SkepticAssessment,
    SkepticInput,
)
from mesh_agents.sota_tracker import BeliefSummary
from mesh_llm.client import LLMResponseError, OllamaNotReadyError

_FIXTURE = Path(__file__).parent / "fixtures" / "llm_responses" / "skeptic_assessment.json"


def _make_input(
    *,
    entity_ids: list[str] | None = None,
    supporting_excerpt: str = "TestModel-7B achieves 87.5% on MMLU",
) -> SkepticInput:
    ent_ids = entity_ids if entity_ids is not None else ["ent-testmodel-7b"]
    return SkepticInput(
        belief=BeliefSummary(
            belief_id="bel-1",
            topic="sota:MMLU",
            statement="TestModel-7B achieves 87.5% on MMLU (as of 2023-06-15)",
            confidence=0.6,
        ),
        supporting_claims=[
            HydratedClaim(
                claim_id="cl-1",
                predicate="achieves_score",
                subject_entity_id="ent-testmodel-7b",
                object={"score": 87.5, "benchmark": "MMLU"},
                raw_excerpt=supporting_excerpt,
                confidence=0.95,
                source_url="https://arxiv.org/abs/2306.00001",
                source_published_at=datetime(2023, 6, 15, tzinfo=UTC),
            ),
        ],
        contradicting_claims=[],
        in_scope_entities=[
            InScopeEntity(entity_id=eid, canonical_name=f"Entity {eid}", type="model")
            for eid in ent_ids
        ],
    )


class MockLLM:
    """Returns canned SkepticAssessment from fixture (or raises on demand)."""

    def __init__(
        self, response_json: str | None = None, raise_exc: Exception | None = None
    ) -> None:
        self._response_json = response_json or _FIXTURE.read_text()
        self._raise_exc = raise_exc

    model = "mock-model"

    def complete_with_latency(
        self,
        name: str,
        system: str,
        user: str,
        response_model: type | None = None,
        options: object | None = None,
    ) -> tuple[object, int]:
        parsed, latency, _ = self.complete_with_usage(
            name, system, user, response_model, options
        )
        return parsed, latency

    def complete_with_usage(
        self,
        name: str,
        system: str,
        user: str,
        response_model: type | None = None,
        options: object | None = None,
    ) -> tuple[object, int, object]:
        from mesh_llm import LLMUsage

        if self._raise_exc is not None:
            raise self._raise_exc
        assert response_model is not None
        try:
            parsed = response_model.model_validate_json(self._response_json)  # type: ignore[attr-defined]
        except Exception as exc:
            raise LLMResponseError(f"mock parse failure: {exc}") from exc
        return parsed, 500, LLMUsage(input_tokens=100, output_tokens=50)


class TestSkepticAgent:
    def _run(self, mock_llm: MockLLM, input: SkepticInput | None = None) -> SkepticAssessment:
        agent = SkepticAgent(llm=mock_llm)  # type: ignore[arg-type]
        result = asyncio.run(agent.run(input or _make_input()))
        assert isinstance(result, SkepticAssessment)
        return result

    def test_happy_path_returns_verdict(self) -> None:
        output = self._run(MockLLM())
        assert output.verdict == "weakened"
        assert output.confidence == pytest.approx(0.78)
        assert output.suggested_confidence_delta == pytest.approx(-0.15)
        assert len(output.counter_claims) == 1

    def test_counter_claim_round_trips(self) -> None:
        output = self._run(MockLLM())
        cc = output.counter_claims[0]
        assert cc.predicate == "achieves_score"
        assert cc.subject_entity_id == "ent-testmodel-7b"
        assert cc.object["score"] == 81.2

    def test_out_of_scope_counter_claims_dropped(self) -> None:
        # The fixture references ent-testmodel-7b — give the input ZERO in-scope entities
        # so the defensive filter strips the counter-claim.
        output = self._run(MockLLM(), input=_make_input(entity_ids=[]))
        assert output.counter_claims == []
        # Rest of the assessment is preserved
        assert output.verdict == "weakened"
        assert output.rationale  # non-empty

    def test_parse_failure_returns_inconclusive_sentinel(self) -> None:
        bad_llm = MockLLM(response_json="not json at all")
        output = self._run(bad_llm)
        assert output.verdict == "inconclusive"
        assert output.confidence == 0.0
        assert output.suggested_confidence_delta == 0.0
        assert output.counter_claims == []

    def test_provider_not_ready_propagates(self) -> None:
        bad_llm = MockLLM(raise_exc=OllamaNotReadyError("offline"))
        agent = SkepticAgent(llm=bad_llm)  # type: ignore[arg-type]
        with pytest.raises(OllamaNotReadyError):
            asyncio.run(agent.run(_make_input()))

    def test_fixture_is_valid_schema(self) -> None:
        data = json.loads(_FIXTURE.read_text())
        assessment = SkepticAssessment.model_validate(data)
        assert assessment.verdict in {"supported", "weakened", "contradicted", "inconclusive"}


class TestClaimBlockProvenance:
    """The system prompt promises source URLs, reliability, and extraction
    dates; the block formatter must actually render them (regression guard for
    the provenance-drop bug)."""

    def test_block_renders_provenance_fields(self) -> None:
        from mesh_agents.skeptic import _format_claim_block

        claim = HydratedClaim(
            claim_id="cl-1",
            predicate="achieves_score",
            subject_entity_id="ent-x",
            object={"score": 87.5, "benchmark": "MMLU"},
            raw_excerpt="X achieves 87.5 on MMLU",
            confidence=0.9,
            source_url="https://arxiv.org/abs/2306.00001",
            source_published_at=datetime(2023, 6, 15, tzinfo=UTC),
            source_reliability=0.8,
            extracted_at=datetime(2024, 1, 2, tzinfo=UTC),
        )
        block = _format_claim_block([claim])
        assert "https://arxiv.org/abs/2306.00001" in block
        assert "source_reliability=0.80" in block
        assert "extracted_at=2024-01-02" in block

    def test_block_marks_missing_provenance_unknown(self) -> None:
        from mesh_agents.skeptic import _format_claim_block

        claim = HydratedClaim(
            claim_id="cl-2",
            predicate="achieves_score",
            subject_entity_id="ent-x",
            object={},
            raw_excerpt="excerpt",
            confidence=0.5,
        )
        block = _format_claim_block([claim])
        assert "source_url=unknown" in block
        assert "source_reliability=unknown" in block
