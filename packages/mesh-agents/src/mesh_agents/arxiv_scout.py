from __future__ import annotations

import asyncio
import hashlib
import os
import re
import threading
from datetime import UTC, datetime
from typing import Any

import arxiv
from mesh_a2a.card_builder import SkillSpec, build_multi_skill_card
from mesh_a2a.task_server import build_task_app
from mesh_models.source import Source, SourceType
from pydantic import BaseModel
from starlette.applications import Starlette

from mesh_agents.base import BaseAgent
from mesh_agents.investigation import (
    InvestigateSkillInput,
    InvestigateSkillOutput,
    investigate_skill_spec,
)


class ScoutedPaper(BaseModel):
    source: Source
    title: str
    abstract: str
    arxiv_id: str


# ── shared arxiv client (rate limiting) ───────────────────────────────────────
# arxiv's API rate-limits hard (HTTP 429) and asks for ~1 request / 3s. The
# `arxiv` package enforces that spacing PER CLIENT via its own `_last_request_dt`
# — but only if we reuse one client. We used to build a fresh `arxiv.Client()`
# per fetch, so the spacing never spanned calls, and the controller dispatches
# several arxiv-touching skills (scout-source, dispatch-investigation)
# concurrently → bursts of unspaced requests → 429 storms (in one run,
# dispatch-investigation errored 65%). Fix: one process-wide client, and a lock
# so the concurrent `asyncio.to_thread` workers issue requests (and take the
# client's rate-limit sleep) one at a time instead of overlapping.
_ARXIV_LOCK = threading.Lock()
_arxiv_client: arxiv.Client | None = None


def _get_arxiv_client() -> arxiv.Client:
    global _arxiv_client
    if _arxiv_client is None:
        _arxiv_client = arxiv.Client(
            page_size=100,
            delay_seconds=float(os.environ.get("MESH_ARXIV_DELAY_SECONDS", "3.0")),
            num_retries=int(os.environ.get("MESH_ARXIV_NUM_RETRIES", "5")),
        )
    return _arxiv_client


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
    cutoff = _utc(since)

    papers: list[ScoutedPaper] = []
    # Serialize the whole paginated fetch through the shared rate-limited client
    # (one arxiv request stream at a time, process-wide) to avoid 429 storms.
    with _ARXIV_LOCK:
        client = _get_arxiv_client()
        for result in client.results(search):
            last_submitted = _utc(result.updated)
            if (
                cutoff is not None
                and last_submitted is not None
                and last_submitted < cutoff
            ):
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


# Phase 7a investigation -------------------------------------------------


# Query words that carry no search signal for arxiv. Two groups: generic English
# stop/question words, and the boilerplate verbs/nouns that LLM-drafted discovery
# hypotheses are riddled with ("describe its architecture, search for papers…").
_QUERY_STOPWORDS = frozenset(
    "a an the of to in on for and or but with without within into onto from by as at "  # noqa: SIM905

    "is are was were be been being do does did has have had can could should would will "
    "what which who whom whose where when why how that this these those it its their there "
    "about across over under between among versus vs compared compare comparison relative "
    "still supported evidence recent latest specific documented established standard "
    "search find describe define definition discuss discussion paper papers arxiv github "
    "repository repositories hackernews news result results benchmark benchmarks evaluation "
    "evaluations methodology methodologies performance performances capability capabilities "
    "limitation limitations application applications system systems approach approaches "
    "model models task tasks used use using e.g i.e etc such other others belief topic".split()
)


def _keywords(text: str, limit: int = 6) -> str:
    """Reduce free text to a short arxiv keyword query.

    arxiv's ``all:`` field ANDs the terms and rejects long natural-language
    questions (HTTP 500/503), so an LLM hypothesis like *"What are the specific
    capability improvements and limitations of GPT-4 compared to GPT-3.5 on
    benchmarks (MMLU, GSM8K, etc.)?"* must be distilled to its content words.

    Strips punctuation, drops stop/boilerplate words and 1-char tokens, dedupes,
    and keeps the most distinctive terms first — entity-like tokens (containing an
    uppercase letter or a digit, e.g. GPT-4, MMLU, GSM8K) rank ahead of plain
    words — capped at ``limit`` so the AND query still matches papers."""
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9.+#-]*", text)
    seen: set[str] = set()
    distinctive: list[str] = []
    plain: list[str] = []
    for tok in tokens:
        low = tok.lower()
        if len(tok) < 2 or low in _QUERY_STOPWORDS or low in seen:
            continue
        seen.add(low)
        if any(c.isupper() for c in tok) or any(c.isdigit() for c in tok):
            distinctive.append(tok)
        else:
            plain.append(tok)
    return " ".join((distinctive + plain)[:limit])


def _query_from_hypothesis(hypothesis: str) -> str:
    """Reduce an investigation hypothesis to arxiv keyword terms.

    Curator hypotheses follow ``… '<statement>' (topic: <topic>) …``; pull those
    out when present. Discovery hypotheses are free-form LLM questions with no such
    structure — for those (and as a final pass over the curator parts) run the text
    through :func:`_keywords` so a whole sentence never reaches arxiv verbatim
    (which 500/503s). Always returns a short keyword query, never a raw question."""
    statement_match = re.search(r"'([^']+)'", hypothesis)
    topic_match = re.search(r"\(topic:\s*([^)]+)\)", hypothesis)
    statement = statement_match.group(1).strip() if statement_match else ""
    topic = topic_match.group(1).strip() if topic_match else ""
    terms = " ".join(t for t in (topic, statement) if t)
    return _keywords(terms or hypothesis)


def _fetch_papers_by_query(query: str, max_results: int) -> list[ScoutedPaper]:
    """Keyword search variant of _fetch_papers used by investigate_arxiv.

    arxiv's API supports free-text queries via the ``all:`` field. ``query``
    is expected to be keyword terms (see ``_query_from_hypothesis``), not a
    natural-language question.
    """
    search = arxiv.Search(
        query=f"all:{query}",
        max_results=max_results,
        sort_by=arxiv.SortCriterion.Relevance,
        sort_order=arxiv.SortOrder.Descending,
    )
    papers: list[ScoutedPaper] = []
    with _ARXIV_LOCK:
        client = _get_arxiv_client()
        for result in client.results(search):
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


async def _handle_investigate_arxiv(payload: dict[str, Any]) -> dict[str, Any]:
    skill_input = InvestigateSkillInput.model_validate(payload)
    query = _query_from_hypothesis(skill_input.hypothesis)
    papers = await asyncio.to_thread(
        _fetch_papers_by_query, query, skill_input.max_results
    )
    return InvestigateSkillOutput(
        investigation_id=skill_input.investigation_id,
        source_records=[p.model_dump(mode="json") for p in papers],
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
        card = build_multi_skill_card(
            name="ArXiv Scout",
            description=(
                "Fetches recent papers from arXiv by category and runs "
                "hypothesis-directed searches."
            ),
            url=url,
            skills=[
                SkillSpec(
                    id="scout_arxiv",
                    name="Scout arXiv",
                    description="Search arXiv for recent papers by category.",
                    tags=["arxiv", "papers", "research"],
                ),
                investigate_skill_spec("arxiv"),
            ],
        )
        return build_task_app(
            agent_card=card,
            skill_handlers={
                "scout_arxiv": _handle_scout_arxiv,
                "investigate_arxiv": _handle_investigate_arxiv,
            },
            agent_name="arxiv_scout",
        )
