"""HN scout — fetches AI/robotics-relevant Hacker News stories via Algolia.

Algolia HN Search API (https://hn.algolia.com/api) was chosen over the
official Firebase API because it returns ready-to-filter JSON (no per-item
lookup loop), supports keyword search out of the box, and needs no auth.

Output shape is the same `ScoutedPaper`-style record the arxiv scout emits,
so ClaimExtractor consumes HN sources unchanged — the abstraction holds.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx
from a2a.helpers.proto_helpers import new_data_artifact, new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import TaskArtifactUpdateEvent, TaskState, TaskStatus, TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict
from mesh_a2a.card_builder import build_agent_card
from mesh_models.source import Source, SourceType
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.routing import Route

from mesh_agents.arxiv_scout import ScoutedPaper
from mesh_agents.base import BaseAgent

logger = logging.getLogger(__name__)

_DEFAULT_KEYWORDS = ["AI", "LLM", "GPT", "Claude", "robotics", "RAG", "agent"]
_ALGOLIA_URL = "https://hn.algolia.com/api/v1/search_by_date"
_DEFAULT_MIN_POINTS = 20  # filter out fly-by posts
_HTTP_TIMEOUT = 15.0


class HNScoutInput(BaseModel):
    keywords: list[str] | None = None
    max_results: int = 20
    min_points: int = _DEFAULT_MIN_POINTS


class HNScoutOutput(BaseModel):
    papers: list[ScoutedPaper]


# Phase 2 A2A skill types ----------------------------------------------------


class ScoutHNSkillInput(BaseModel):
    keywords: list[str] | None = None
    max_results: int = 20
    min_points: int = _DEFAULT_MIN_POINTS


class ScoutHNSkillOutput(BaseModel):
    papers: list[dict[str, Any]]


# Shared fetch logic ---------------------------------------------------------


def _make_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _resolve_keywords(explicit: list[str] | None) -> list[str]:
    if explicit:
        return explicit
    env = os.environ.get("MESH_HN_KEYWORDS")
    if env:
        return [k.strip() for k in env.split(",") if k.strip()]
    return list(_DEFAULT_KEYWORDS)


def _fetch_one(
    client: httpx.Client, keyword: str, per_keyword: int, min_points: int
) -> list[ScoutedPaper]:
    """One Algolia round-trip per keyword. Filters by points and tag=story."""
    params: dict[str, str | int] = {
        "query": keyword,
        "tags": "story",
        "numericFilters": f"points>={min_points}",
        "hitsPerPage": per_keyword,
    }
    response = client.get(_ALGOLIA_URL, params=params, timeout=_HTTP_TIMEOUT)
    response.raise_for_status()
    hits = response.json().get("hits", [])

    papers: list[ScoutedPaper] = []
    for hit in hits:
        # Either a submitted URL or an Ask HN / Show HN with text content.
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}"
        text = (hit.get("story_text") or hit.get("comment_text") or "").strip()
        title = (hit.get("title") or hit.get("story_title") or "").strip()
        # If there's no body text, fall back to the title as the "abstract" — claims
        # extractor still gets something to work with for link posts.
        abstract = text or title
        if not abstract:
            continue

        created_at_raw = hit.get("created_at")
        try:
            published_at = (
                datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
                if created_at_raw
                else datetime.now(UTC)
            )
        except ValueError:
            published_at = datetime.now(UTC)

        source = Source(
            type=SourceType.hn_post,
            url=url,
            author=hit.get("author"),
            published_at=published_at,
            raw_content_hash=_make_hash(abstract),
        )
        papers.append(
            ScoutedPaper(
                source=source,
                title=title or url,
                abstract=abstract,
                # Reuse the arxiv_id slot to carry the HN object id — it's the
                # natural per-source identifier and downstream code only treats
                # it as a string.
                arxiv_id=str(hit["objectID"]),
            )
        )
    return papers


def _fetch_hn(
    keywords: list[str] | None, max_results: int, min_points: int
) -> list[ScoutedPaper]:
    resolved = _resolve_keywords(keywords)
    # Spread the budget evenly across keywords, with a floor of 1.
    per_keyword = max(max_results // max(len(resolved), 1), 1)

    seen_hashes: set[str] = set()
    collected: list[ScoutedPaper] = []
    with httpx.Client() as client:
        for keyword in resolved:
            try:
                for paper in _fetch_one(client, keyword, per_keyword, min_points):
                    if paper.source.raw_content_hash in seen_hashes:
                        continue
                    seen_hashes.add(paper.source.raw_content_hash)
                    collected.append(paper)
                    if len(collected) >= max_results:
                        return collected
            except httpx.HTTPError as exc:
                # One keyword failing shouldn't abort the scout — log and continue.
                logger.warning(
                    "hn_scout_keyword_failed",
                    extra={"keyword": keyword, "error": str(exc)},
                )

    return collected


# A2A executor ---------------------------------------------------------------


class _HNScoutExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        assert context.message is not None
        task = new_task_from_user_message(context.message)
        await event_queue.enqueue_event(task)

        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
            )
        )

        raw: dict[str, Any] = {}
        for part in context.message.parts:
            if part.HasField("data"):
                raw = dict(MessageToDict(part.data))
                break
        skill_input = ScoutHNSkillInput.model_validate(raw)

        papers = await asyncio.to_thread(
            _fetch_hn,
            skill_input.keywords,
            skill_input.max_results,
            skill_input.min_points,
        )

        output = ScoutHNSkillOutput(papers=[p.model_dump(mode="json") for p in papers])

        await event_queue.enqueue_event(
            TaskArtifactUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                artifact=new_data_artifact("result", output.model_dump(mode="json")),
            )
        )
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("cancel not supported")


# Agent ----------------------------------------------------------------------


class HNScoutAgent(BaseAgent):
    name = "hn_scout"

    def __init__(self, llm: Any | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> HNScoutOutput:
        assert isinstance(input, HNScoutInput)
        papers = await asyncio.to_thread(
            _fetch_hn, input.keywords, input.max_results, input.min_points
        )
        return HNScoutOutput(papers=papers)

    def to_a2a_server(self, url: str) -> Starlette:
        card = build_agent_card(
            name="HN Scout",
            description="Fetches AI/robotics-relevant stories from Hacker News via Algolia.",
            url=url,
            skill_id="scout_hn",
            skill_name="Scout Hacker News",
            skill_description="Search Hacker News for recent AI/robotics-relevant stories.",
            skill_tags=["hackernews", "hn", "stories"],
        )
        handler = DefaultRequestHandler(
            agent_executor=_HNScoutExecutor(),
            task_store=InMemoryTaskStore(),
            agent_card=card,
        )
        routes: list[Route] = []
        routes.extend(create_agent_card_routes(card))
        routes.extend(create_jsonrpc_routes(handler, "/"))
        return Starlette(routes=routes)
