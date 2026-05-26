from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any

import arxiv
from mesh_a2a.card_builder import build_agent_card
from mesh_a2a.task_server import build_task_app
from mesh_models.source import Source, SourceType
from pydantic import BaseModel
from starlette.applications import Starlette

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


# ---------------------------------------------------------------------------
# Phase 2 A2A skill types
# ---------------------------------------------------------------------------


class ScoutArxivSkillInput(BaseModel):
    categories: list[str] = ["cs.AI", "cs.RO", "cs.LG"]
    max_results: int = 20
    since: str | None = None  # ISO-8601 string


class ScoutArxivSkillOutput(BaseModel):
    papers: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Shared fetch logic
# ---------------------------------------------------------------------------


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
        last_submitted = _utc(result.updated)
        if cutoff is not None and last_submitted is not None and last_submitted < cutoff:
            break

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


async def _handle_scout_arxiv(payload: dict[str, Any]) -> dict[str, Any]:
    skill_input = ScoutArxivSkillInput.model_validate(payload)
    since: datetime | None = None
    if skill_input.since:
        since = datetime.fromisoformat(skill_input.since)
    papers = await asyncio.to_thread(
        _fetch_papers, skill_input.categories, skill_input.max_results, since
    )
    return ScoutArxivSkillOutput(
        papers=[p.model_dump(mode="json") for p in papers]
    ).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


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

    def to_a2a_server(self, url: str) -> Starlette:
        card = build_agent_card(
            name="ArXiv Scout",
            description="Fetches recent papers from arXiv by category.",
            url=url,
            skill_id="scout_arxiv",
            skill_name="Scout arXiv",
            skill_description="Search arXiv for recent papers by category.",
            skill_tags=["arxiv", "papers", "research"],
        )
        return build_task_app(
            agent_card=card,
            skill_handlers={"scout_arxiv": _handle_scout_arxiv},
            agent_name="arxiv_scout",
        )
