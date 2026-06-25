from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import mesh_agents.arxiv_scout as arxiv_scout
import pytest
from mesh_agents.arxiv_scout import (
    ArxivScoutAgent,
    ArxivScoutInput,
    ScoutedPaper,
    _make_hash,
    _query_from_hypothesis,
)


@pytest.fixture(autouse=True)
def _reset_arxiv_client() -> Iterator[None]:
    """The shared arxiv client is cached in a module global; reset it so each
    test rebuilds it under its own patched ``arxiv.Client`` mock."""
    arxiv_scout._arxiv_client = None
    yield
    arxiv_scout._arxiv_client = None


class TestQueryFromHypothesis:
    """investigate_arxiv must search on keyword terms, not the Curator's
    natural-language question (regression guard for the question-as-query bug)."""

    def test_extracts_statement_and_topic_as_keywords(self) -> None:
        hypothesis = (
            "Is the belief 'GR00T N1 achieves 78% on RoboArena' "
            "(topic: robot policy) still supported by recent evidence?"
        )
        query = _query_from_hypothesis(hypothesis)
        # Entity/benchmark terms survive; English scaffolding + punctuation gone.
        assert "RoboArena" in query
        assert "GR00T" in query
        assert "still supported" not in query
        assert "Is the belief" not in query
        assert "'" not in query and "?" not in query
        assert len(query.split()) <= 6

    def test_reduces_natural_language_question(self) -> None:
        # Discovery hypotheses are free-form questions; arxiv 500/503s on these
        # verbatim. They must become a short keyword query.
        q = _query_from_hypothesis(
            "What are the specific capability improvements and limitations of "
            "GPT-4 compared to GPT-3.5 on benchmarks (MMLU, GSM8K, HumanEval, etc.)?"
        )
        assert "GPT-4" in q and "MMLU" in q
        assert "?" not in q and "(" not in q and "/" not in q
        assert len(q.split()) <= 6
        for stop in ("what", "are", "the", "compared", "benchmarks"):
            assert stop not in q.lower().split()

    def test_falls_back_to_keywords_when_unstructured(self) -> None:
        assert _query_from_hypothesis("plain keywords") == "plain keywords"


def _fake_result(
    arxiv_id: str = "2401.00001v1",
    title: str = "Test Paper",
    summary: str = "This is the abstract.",
    published: datetime | None = None,
    updated: datetime | None = None,
) -> MagicMock:
    result = MagicMock()
    result.entry_id = f"https://arxiv.org/abs/{arxiv_id}"
    result.title = title
    result.summary = summary
    pub = published or datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
    result.published = pub
    result.updated = updated or pub  # default: updated == published (v1 only)
    result.authors = [MagicMock(name="Jane Doe")]
    result.authors[0].name = "Jane Doe"
    return result


class TestArxivScoutAgent:
    def _run(self, input: ArxivScoutInput, fake_results: list[MagicMock]) -> list[ScoutedPaper]:
        agent = ArxivScoutAgent()
        with patch("mesh_agents.arxiv_scout.arxiv.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.results.return_value = iter(fake_results)
            mock_cls.return_value = mock_client
            output = asyncio.run(agent.run(input))
        return output.papers

    def test_returns_scouted_papers(self) -> None:
        papers = self._run(
            ArxivScoutInput(categories=["cs.AI"], max_results=5),
            [_fake_result("2401.00001v1", "Alpha", "Alpha abstract")],
        )
        assert len(papers) == 1
        assert papers[0].title == "Alpha"
        assert papers[0].arxiv_id == "2401.00001v1"

    def test_hash_determinism(self) -> None:
        abstract = "Deterministic abstract text."
        h1 = _make_hash(abstract)
        h2 = _make_hash(abstract)
        assert h1 == h2
        assert len(h1) == 64  # sha256 hex

    def test_shared_client_reused_across_fetches(self) -> None:
        """Both fetch paths must reuse ONE arxiv client so its per-client 3s
        rate-limit spans calls (the 429-storm fix)."""
        from mesh_agents.arxiv_scout import _fetch_papers, _fetch_papers_by_query

        with patch("mesh_agents.arxiv_scout.arxiv.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.results.return_value = iter([])
            mock_cls.return_value = mock_client
            _fetch_papers(["cs.AI"], 5, None)
            mock_client.results.return_value = iter([])
            _fetch_papers_by_query("foo", 5)
            assert mock_cls.call_count == 1  # one shared client, not per-call

    def test_source_url_is_arxiv_abstract(self) -> None:
        papers = self._run(
            ArxivScoutInput(categories=["cs.AI"], max_results=5),
            [_fake_result("2401.12345v2")],
        )
        assert papers[0].source.url == "https://arxiv.org/abs/2401.12345v2"

    def test_source_type_is_arxiv(self) -> None:
        papers = self._run(
            ArxivScoutInput(categories=["cs.AI"], max_results=5),
            [_fake_result()],
        )
        from mesh_models.source import SourceType

        assert papers[0].source.type == SourceType.arxiv

    def test_filters_by_since(self) -> None:
        # Results must be in descending date order (as arxiv returns them) so
        # the break-on-first-old-result logic works correctly.
        new = _fake_result("new", published=datetime(2024, 6, 1, tzinfo=UTC))
        old = _fake_result("old", published=datetime(2023, 1, 1, tzinfo=UTC))

        papers = self._run(
            ArxivScoutInput(
                categories=["cs.AI"],
                max_results=10,
                since=datetime(2024, 1, 1, tzinfo=UTC),
            ),
            [new, old],
        )
        ids = [p.arxiv_id for p in papers]
        assert "new" in ids
        assert "old" not in ids

    def test_filters_by_updated_not_published(self) -> None:
        # A paper originally published in 2022 but updated recently should pass
        # the since filter because filtering uses result.updated.
        recently_revised = _fake_result(
            "revised",
            published=datetime(2022, 1, 1, tzinfo=UTC),
            updated=datetime(2024, 6, 1, tzinfo=UTC),
        )
        papers = self._run(
            ArxivScoutInput(
                categories=["cs.AI"],
                max_results=10,
                since=datetime(2024, 1, 1, tzinfo=UTC),
            ),
            [recently_revised],
        )
        assert len(papers) == 1
        assert papers[0].arxiv_id == "revised"

    def test_max_results_cap_when_filtering(self) -> None:
        results = [
            _fake_result(f"p{i}", published=datetime(2024, 6, i + 1, tzinfo=UTC))
            for i in range(5)
        ]
        papers = self._run(
            ArxivScoutInput(
                categories=["cs.AI"],
                max_results=3,
                since=datetime(2024, 1, 1, tzinfo=UTC),
            ),
            results,
        )
        assert len(papers) == 3

    def test_abstract_newlines_normalized(self) -> None:
        papers = self._run(
            ArxivScoutInput(categories=["cs.AI"], max_results=5),
            [_fake_result(summary="Line one.\nLine two.\nLine three.")],
        )
        assert "\n" not in papers[0].abstract
        assert "Line one." in papers[0].abstract

    def test_empty_results(self) -> None:
        papers = self._run(ArxivScoutInput(categories=["cs.AI"], max_results=5), [])
        assert papers == []

    def test_author_captured(self) -> None:
        papers = self._run(
            ArxivScoutInput(categories=["cs.AI"], max_results=5),
            [_fake_result()],
        )
        assert papers[0].source.author == "Jane Doe"
