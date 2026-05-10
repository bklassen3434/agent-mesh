from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import arxiv
from a2a.helpers.proto_helpers import new_data_artifact, new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue_v2 import EventQueue
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

from mesh_agents.base import BaseAgent

if TYPE_CHECKING:
    pass


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


# ---------------------------------------------------------------------------
# A2A executor
# ---------------------------------------------------------------------------


class _ArxivScoutExecutor(AgentExecutor):
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

        # Parse skill input
        raw: dict[str, Any] = {}
        for part in context.message.parts:
            if part.HasField("data"):
                raw = dict(MessageToDict(part.data))
                break
        skill_input = ScoutArxivSkillInput.model_validate(raw)

        since: datetime | None = None
        if skill_input.since:
            since = datetime.fromisoformat(skill_input.since)

        papers = await asyncio.to_thread(
            _fetch_papers, skill_input.categories, skill_input.max_results, since
        )

        output = ScoutArxivSkillOutput(
            papers=[p.model_dump(mode="json") for p in papers]
        )

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
        handler = DefaultRequestHandler(
            agent_executor=_ArxivScoutExecutor(),
            task_store=InMemoryTaskStore(),
            agent_card=card,
        )
        routes: list[Route] = []
        routes.extend(create_agent_card_routes(card))
        routes.extend(create_jsonrpc_routes(handler, "/"))
        return Starlette(routes=routes)
