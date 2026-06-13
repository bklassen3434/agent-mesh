"""Web search scout — Brave Search config-driven connector (Phase 18a).

A *config-driven* connector: one service serves arbitrarily many configured
instances. Each instance is a ``field_connectors`` row whose config supplies
``web_seed_queries``. This is the universal fallback — any field can ingest on
day one with nothing but a few search queries — and also backs the
investigation path (``investigate_web``) with a hypothesis-directed search.

Backed by the Brave Search API (``BRAVE_API_KEY``). Result snippets
(description + extra_snippets) are used as the source body; pages are NOT
fetched, which keeps the connector read-only, bounded, and off the
arbitrary-URL SSRF surface. A missing key degrades to an empty result so the
run continues.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx
from mesh_a2a.card_builder import SkillSpec, build_multi_skill_card
from mesh_a2a.task_server import build_task_app
from mesh_models.source import Source, SourceType
from pydantic import BaseModel
from starlette.applications import Starlette

from mesh_agents.arxiv_scout import ScoutedPaper
from mesh_agents.base import BaseAgent
from mesh_agents.investigation import (
    InvestigateSkillInput,
    InvestigateSkillOutput,
    investigate_skill_spec,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 20.0
_BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
_BRAVE_MAX_COUNT = 20  # Brave's per-request cap
_MAX_ABSTRACT_LEN = 4000


class ScoutWebSearchSkillInput(BaseModel):
    web_seed_queries: list[str] = []
    max_results: int = 20
    since: str | None = None  # ISO-8601; results older than it are skipped


class ScoutWebSearchSkillOutput(BaseModel):
    papers: list[dict[str, Any]]


def _make_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _api_key() -> str | None:
    return os.environ.get("BRAVE_API_KEY") or None


def _result_abstract(result: dict[str, Any]) -> str:
    """Brave gives a snippet (description) plus optional extra_snippets; join
    them for a richer body without fetching the page."""
    parts: list[str] = []
    desc = (result.get("description") or "").strip()
    if desc:
        parts.append(desc)
    for snippet in result.get("extra_snippets") or []:
        if isinstance(snippet, str) and snippet.strip():
            parts.append(snippet.strip())
    return " ".join(parts)[:_MAX_ABSTRACT_LEN]


def _result_published(result: dict[str, Any]) -> datetime:
    raw = result.get("page_age") or result.get("age")
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def _result_to_paper(result: dict[str, Any], cutoff: datetime | None) -> ScoutedPaper | None:
    if not isinstance(result, dict):
        return None
    url = (result.get("url") or "").strip()
    title = (result.get("title") or "").strip()
    abstract = _result_abstract(result) or title
    if not url or not abstract:
        return None

    published = _result_published(result)
    if cutoff is not None and published < cutoff:
        return None

    source = Source(
        type=SourceType.web,
        url=url,
        published_at=published,
        raw_content_hash=_make_hash(abstract),
    )
    return ScoutedPaper(
        source=source,
        title=title or url,
        abstract=abstract,
        arxiv_id=f"web_{_make_hash(url)[:12]}",
    )


def _search_one(client: httpx.Client, query: str, count: int) -> list[dict[str, Any]]:
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": _api_key() or "",
    }
    params: dict[str, str | int] = {"q": query, "count": min(count, _BRAVE_MAX_COUNT)}
    try:
        resp = client.get(_BRAVE_URL, headers=headers, params=params, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("web_search_failed", extra={"query": query, "error": str(exc)})
        return []
    results = (data.get("web") or {}).get("results") or []
    return results if isinstance(results, list) else []


def _search(
    queries: list[str], max_results: int, cutoff: datetime | None
) -> list[ScoutedPaper]:
    if not _api_key():
        logger.warning("web_search_no_api_key")
        return []
    seen: set[str] = set()
    out: list[ScoutedPaper] = []
    with httpx.Client() as client:
        for query in queries:
            if len(out) >= max_results:
                break
            for result in _search_one(client, query, max_results):
                paper = _result_to_paper(result, cutoff)
                if paper is None or paper.source.url in seen:
                    continue
                seen.add(paper.source.url)
                out.append(paper)
                if len(out) >= max_results:
                    break
    return out


async def _handle_scout_web_search(payload: dict[str, Any]) -> dict[str, Any]:
    skill_input = ScoutWebSearchSkillInput.model_validate(payload)
    cutoff: datetime | None = None
    if skill_input.since:
        cutoff = datetime.fromisoformat(skill_input.since)
    papers = await asyncio.to_thread(
        _search, skill_input.web_seed_queries, skill_input.max_results, cutoff
    )
    return ScoutWebSearchSkillOutput(
        papers=[p.model_dump(mode="json") for p in papers]
    ).model_dump(mode="json")


async def _handle_investigate_web(payload: dict[str, Any]) -> dict[str, Any]:
    """Hypothesis-directed web search. Web search handles natural-language
    queries well, so the hypothesis text is used as the query directly."""
    skill_input = InvestigateSkillInput.model_validate(payload)
    papers = await asyncio.to_thread(
        _search, [skill_input.hypothesis], skill_input.max_results, None
    )
    return InvestigateSkillOutput(
        investigation_id=skill_input.investigation_id,
        source_records=[p.model_dump(mode="json") for p in papers],
    ).model_dump(mode="json")


class WebSearchScoutAgent(BaseAgent):
    name = "web_search_scout"

    def __init__(self, llm: Any | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> ScoutWebSearchSkillOutput:  # pragma: no cover
        raise NotImplementedError("WebSearchScoutAgent uses the A2A skill path only")

    def to_a2a_server(self, url: str) -> Starlette:
        card = build_multi_skill_card(
            name="Web Search Scout",
            description="Brave web search over seed queries; the universal fallback source.",
            url=url,
            skills=[
                SkillSpec(
                    id="scout_web_search",
                    name="Scout Web Search",
                    description="Run Brave web searches over web_seed_queries.",
                    tags=["web", "search", "brave", "config_driven"],
                ),
                # SourceType.web == "web", so the investigation dispatch
                # (investigate_<source_type>) resolves to investigate_web.
                investigate_skill_spec("web"),
            ],
        )
        return build_task_app(
            agent_card=card,
            skill_handlers={
                "scout_web_search": _handle_scout_web_search,
                "investigate_web": _handle_investigate_web,
            },
            agent_name="web_search_scout",
        )
