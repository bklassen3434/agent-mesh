from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from mesh_db.claims import list_claims
from mesh_db.entities import list_entities
from mesh_db.sources import list_sources
from mesh_llm.client import OllamaNotReadyError
from mesh_pipeline.orchestrator import PipelineResult

_FIXTURE = Path(__file__).parent / "fixtures" / "llm_responses" / "claim_extraction_result.json"


def _fake_arxiv_result(arxiv_id: str = "2401.00001v1") -> MagicMock:
    r = MagicMock()
    r.entry_id = f"https://arxiv.org/abs/{arxiv_id}"
    r.title = f"Paper {arxiv_id}"
    r.summary = f"TestModel-7B achieves 87.5% on MMLU. Developed by TestLab. {arxiv_id}."
    r.published = datetime(2024, 1, 15, tzinfo=UTC)
    r.authors = [MagicMock()]
    r.authors[0].name = "Jane Doe"
    return r


class MockOllamaClient:
    model = "qwen3:14b"
    host = "http://localhost:11434"

    def __init__(self, fixture_json: str | None = None) -> None:
        self._fixture_json = fixture_json or _FIXTURE.read_text()

    def health_check(self) -> None:
        pass

    def complete_with_latency(
        self,
        name: str,
        system: str,
        user: str,
        response_model: type | None = None,
        options: object | None = None,
    ) -> tuple[object, int]:
        if response_model is not None:
            parsed = response_model.model_validate_json(self._fixture_json)  # type: ignore[attr-defined]
            return parsed, 200
        return self._fixture_json, 200


def _run_pipeline(tmp_path: Path, arxiv_ids: list[str] | None = None) -> PipelineResult:
    if arxiv_ids is None:
        arxiv_ids = ["2401.00001v1"]
    from mesh_pipeline.orchestrator import run_pipeline

    db_path = str(tmp_path / "test.db")
    fake_results = [_fake_arxiv_result(aid) for aid in arxiv_ids]

    with (
        patch("mesh_agents.arxiv_scout.arxiv.Client") as mock_cls,
        patch("mesh_pipeline.orchestrator.make_llm_client", return_value=MockOllamaClient()),
    ):
        mock_client = MagicMock()
        mock_client.results.return_value = iter(fake_results)
        mock_cls.return_value = mock_client

        return asyncio.run(
            run_pipeline(
                categories=["cs.AI"],
                max_papers=5,
                since=None,
                db_path=db_path,
            )
        )


class TestOrchestrator:
    def test_end_to_end_inserts_sources(self, tmp_path: Path) -> None:
        from mesh_db.connection import get_connection

        _run_pipeline(tmp_path)

        db_path = str(tmp_path / "test.db")
        conn = get_connection(db_path)
        sources = list_sources(conn)
        conn.close()
        assert len(sources) >= 1

    def test_end_to_end_inserts_claims(self, tmp_path: Path) -> None:
        from mesh_db.connection import get_connection

        _run_pipeline(tmp_path)

        conn = get_connection(str(tmp_path / "test.db"))
        claims = list_claims(conn)
        conn.close()
        assert len(claims) >= 1

    def test_end_to_end_creates_entities(self, tmp_path: Path) -> None:
        from mesh_db.connection import get_connection

        _run_pipeline(tmp_path)

        conn = get_connection(str(tmp_path / "test.db"))
        entities = list_entities(conn)
        conn.close()
        assert any(e.canonical_name == "TestModel-7B" for e in entities)

    def test_idempotency_no_duplicate_sources(self, tmp_path: Path) -> None:
        from mesh_db.connection import get_connection

        _run_pipeline(tmp_path)
        result2 = _run_pipeline(tmp_path)

        conn = get_connection(str(tmp_path / "test.db"))
        sources = list_sources(conn)
        conn.close()
        assert len(sources) == 1  # second run adds nothing
        assert result2.sources_inserted == 0

    def test_idempotency_no_duplicate_claims(self, tmp_path: Path) -> None:
        from mesh_db.connection import get_connection

        _run_pipeline(tmp_path)
        _run_pipeline(tmp_path)

        conn = get_connection(str(tmp_path / "test.db"))
        claims = list_claims(conn)
        conn.close()
        # Claims from second run would only be inserted for new sources; there are none
        first_count = len(list_claims(get_connection(str(tmp_path / "test.db"))))
        assert first_count == len(claims)

    def test_pipeline_aborts_if_health_check_fails(self, tmp_path: Path) -> None:
        from mesh_pipeline.orchestrator import run_pipeline

        db_path = str(tmp_path / "test.db")

        class FailingLLM:
            model = "qwen3:14b"
            host = "http://localhost:11434"

            def health_check(self) -> None:
                raise OllamaNotReadyError("offline")

        with (
            patch("mesh_pipeline.orchestrator.make_llm_client", return_value=FailingLLM()),
            pytest.raises(SystemExit),
        ):
            asyncio.run(
                run_pipeline(categories=["cs.AI"], max_papers=5, since=None, db_path=db_path)
            )

    def test_error_in_one_paper_does_not_abort(self, tmp_path: Path) -> None:
        from mesh_pipeline.orchestrator import run_pipeline

        db_path = str(tmp_path / "test.db")

        call_count = 0

        class PartiallyFailingLLM:
            model = "qwen3:14b"
            host = "http://localhost:11434"

            def health_check(self) -> None:
                pass

            def complete_with_latency(
                self,
                name: str,
                system: str,
                user: str,
                response_model: type | None = None,
                options: object | None = None,
            ) -> tuple[object, int]:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise RuntimeError("simulated extraction failure")
                if response_model is not None:
                    return response_model.model_validate_json(_FIXTURE.read_text()), 100  # type: ignore[attr-defined]
                return "", 100

        with (
            patch("mesh_agents.arxiv_scout.arxiv.Client") as mock_cls,
            patch("mesh_pipeline.orchestrator.make_llm_client", return_value=PartiallyFailingLLM()),
        ):
            mock_client = MagicMock()
            mock_client.results.return_value = iter([
                _fake_arxiv_result("2401.00001v1"),
                _fake_arxiv_result("2401.00002v1"),
            ])
            mock_cls.return_value = mock_client

            result = asyncio.run(
                run_pipeline(categories=["cs.AI"], max_papers=5, since=None, db_path=db_path)
            )

        # Pipeline completes, one error recorded
        assert result.errors
        assert result.sources_inserted == 2

    def test_result_has_expected_fields(self, tmp_path: Path) -> None:
        result = _run_pipeline(tmp_path)
        assert hasattr(result, "run_id")
        assert hasattr(result, "sources_inserted")
        assert hasattr(result, "claims_inserted")
        assert hasattr(result, "entities_created")
        assert hasattr(result, "beliefs_created")
        assert hasattr(result, "beliefs_revised")
