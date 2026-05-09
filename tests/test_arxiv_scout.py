from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from mesh_agents.arxiv_scout import ArxivScoutAgent, ArxivScoutInput, ScoutedPaper, _make_hash


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
