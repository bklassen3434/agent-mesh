from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest
from mesh_agents.arxiv_scout import ScoutedPaper
from mesh_agents.claim_extractor import (
    ClaimExtractionResult,
    ClaimExtractorAgent,
    ClaimExtractorInput,
    ClaimExtractorOutput,
)
from mesh_llm.client import LLMResponseError, OllamaNotReadyError
from mesh_models.source import Source, SourceType

_FIXTURE = Path(__file__).parent / "fixtures" / "llm_responses" / "claim_extraction_result.json"


def _make_paper() -> ScoutedPaper:
    source = Source(
        type=SourceType.arxiv,
        url="https://arxiv.org/abs/2401.00001",
        published_at=datetime(2024, 1, 15),
        raw_content_hash="abc123",
    )
    return ScoutedPaper(
        source=source,
        title="TestModel-7B: A Strong Baseline",
        abstract=(
            "TestModel-7B achieves 87.5% accuracy on MMLU, "
            "outperforming GPT-3. Developed by TestLab."
        ),
        arxiv_id="2401.00001",
    )


class MockOllamaClient:
    """Returns canned ClaimExtractionResult from fixture."""

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
        usage = LLMUsage(input_tokens=120, output_tokens=60)
        if response_model is not None:
            try:
                parsed = response_model.model_validate_json(self._response_json)  # type: ignore[attr-defined]
            except Exception as exc:
                raise LLMResponseError(f"mock parse failure: {exc}") from exc
            return parsed, 500, usage
        return self._response_json, 500, usage


class TestClaimExtractorAgent:
    def _run(self, mock_llm: MockOllamaClient) -> ClaimExtractorOutput:
        agent = ClaimExtractorAgent(llm=mock_llm)  # type: ignore[arg-type]
        result = asyncio.run(agent.run(ClaimExtractorInput(paper=_make_paper())))
        assert isinstance(result, ClaimExtractorOutput)
        return result

    def test_happy_path_returns_claims(self) -> None:
        output = self._run(MockOllamaClient())
        assert len(output.claims) == 4

    def test_predicate_types(self) -> None:
        output = self._run(MockOllamaClient())
        predicates = {c.predicate for c in output.claims}
        assert "achieves_score" in predicates
        assert "outperforms" in predicates
        assert "developed_by" in predicates
        assert "evaluated_on" in predicates

    def test_entities_referenced_deduped(self) -> None:
        output = self._run(MockOllamaClient())
        # All four claims have the same subject_name
        assert output.entities_referenced == ["TestModel-7B"]

    def test_parse_failure_returns_empty_not_exception(self) -> None:
        bad_llm = MockOllamaClient(response_json="not json at all oops")
        agent = ClaimExtractorAgent(llm=bad_llm)  # type: ignore[arg-type]
        output = asyncio.run(agent.run(ClaimExtractorInput(paper=_make_paper())))
        assert output.claims == []
        assert output.entities_referenced == []

    def test_ollama_connection_failure_raises(self) -> None:
        bad_llm = MockOllamaClient(raise_exc=OllamaNotReadyError("offline"))
        agent = ClaimExtractorAgent(llm=bad_llm)  # type: ignore[arg-type]
        with pytest.raises(OllamaNotReadyError):
            asyncio.run(agent.run(ClaimExtractorInput(paper=_make_paper())))

    def test_latency_returned(self) -> None:
        output = self._run(MockOllamaClient())
        assert output.latency_ms == 500

    def test_fixture_is_valid_schema(self) -> None:
        data = json.loads(_FIXTURE.read_text())
        result = ClaimExtractionResult.model_validate(data)
        assert len(result.claims) == 4
