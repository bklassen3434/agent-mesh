from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any

import arxiv
from mesh_models.source import Source, SourceType
from pydantic import BaseModel

from mesh_agents.base import BaseAgent


class ScoutedPaper(BaseModel):
    source: Source
    title: str
    abstract: str
    arxiv_id: str


class ArxivScoutInput(BaseModel):
    categories: list[str] = ["cs.AI", "cs.RO", "cs.LG"]
    max_results: int = 20
    since: datetime | None = None


class ArxivScoutOutput(BaseModel):
    papers: list[ScoutedPaper]


def _build_query(categories: list[str]) -> str:
    return " OR ".join(f"cat:{cat}" for cat in categories)


def _make_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _fetch_papers(
    categories: list[str],
    max_results: int,
    since: datetime | None,
) -> list[ScoutedPaper]:
    query = _build_query(categories)
    # When date-filtering, request a larger page so the max_results cap on the
    # *output* isn't silently hit before we've scanned past the cutoff.
    arxiv_max = max_results if since is None else max(max_results * 5, 200)
    search = arxiv.Search(
        query=query,
        max_results=arxiv_max,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )
    client = arxiv.Client()
    cutoff = _utc(since)

    papers: list[ScoutedPaper] = []
    for result in client.results(search):
        # arxiv sorts by last-submission date (result.updated), not the original
        # publication date (result.published). Compare against updated so that
        # revised papers aren't incorrectly excluded.
        last_submitted = _utc(result.updated)
        if cutoff is not None and last_submitted is not None and last_submitted < cutoff:
            break  # results are sorted descending — everything after is older

        arxiv_id = result.entry_id.split("/")[-1]
        url = f"https://arxiv.org/abs/{arxiv_id}"
        abstract = result.summary.replace("\n", " ")
        author = result.authors[0].name if result.authors else None

        source = Source(
            type=SourceType.arxiv,
            url=url,
            author=author,
            published_at=result.published or datetime.now(UTC),
            raw_content_hash=_make_hash(abstract),
        )
        papers.append(
            ScoutedPaper(
                source=source,
                title=result.title,
                abstract=abstract,
                arxiv_id=arxiv_id,
            )
        )

        if len(papers) >= max_results:
            break

    return papers


class ArxivScoutAgent(BaseAgent):
    name = "arxiv_scout"

    def __init__(self, llm: Any | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> ArxivScoutOutput:
        assert isinstance(input, ArxivScoutInput)
        papers = await asyncio.to_thread(
            _fetch_papers, input.categories, input.max_results, input.since
        )
        return ArxivScoutOutput(papers=papers)
