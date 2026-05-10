from __future__ import annotations

import contextlib
from typing import Any

import duckdb
from a2a.helpers.proto_helpers import new_data_artifact, new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import TaskArtifactUpdateEvent, TaskState, TaskStatus, TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict
from mesh_a2a.card_builder import build_agent_card
from mesh_db.entities import create_entity
from mesh_models.entity import Entity, EntityType
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.routing import Route

from mesh_agents.base import BaseAgent

# ---------------------------------------------------------------------------
# Phase 1 types (DB-backed, unchanged)
# ---------------------------------------------------------------------------


class EntityTrackerInput(BaseModel):
    names: list[str]
    type_hints: dict[str, EntityType] | None = None


class EntityTrackerOutput(BaseModel):
    resolved: dict[str, str]  # name → entity_id
    created_count: int = 0


def _find_entity_by_name(conn: duckdb.DuckDBPyConnection, name: str) -> str | None:
    """Return entity_id if found by canonical_name or alias (case-insensitive)."""
    row = conn.execute(
        """
        SELECT id FROM entities
        WHERE lower(canonical_name) = lower(?)
           OR len(list_filter(aliases, x -> lower(x) = lower(?))) > 0
        LIMIT 1
        """,
        [name, name],
    ).fetchone()
    return str(row[0]) if row else None


# ---------------------------------------------------------------------------
# Phase 2 types (pure, no DB)
# ---------------------------------------------------------------------------


class EntitySummary(BaseModel):
    """Lightweight view of an existing entity, passed in from the coordinator."""

    entity_id: str
    canonical_name: str
    aliases: list[str] = []
    entity_type: str = EntityType.concept.value


class ResolvedEntityInfo(BaseModel):
    """Resolution result for a single candidate name."""

    name: str
    entity_id: str
    canonical_name: str
    entity_type: str
    is_new: bool


class EntityResolveSkillInput(BaseModel):
    """Input for the resolve_entities A2A skill."""

    candidate_names: list[str]
    existing_entities: list[EntitySummary] = []
    type_hints: dict[str, str] | None = None  # name → EntityType value


class EntityResolveSkillOutput(BaseModel):
    """Output for the resolve_entities A2A skill."""

    resolved: list[ResolvedEntityInfo]


def resolve_entities_pure(
    candidate_names: list[str],
    existing_entities: list[EntitySummary],
    type_hints: dict[str, str] | None = None,
) -> list[ResolvedEntityInfo]:
    """Pure entity resolution — no DB access.

    Matches candidate names against existing_entities (case-insensitive, by
    canonical_name or alias). Unmatched names get a new UUID-bearing entity.
    The coordinator is responsible for persisting is_new=True entities to DB.
    """
    result: list[ResolvedEntityInfo] = []
    for name in candidate_names:
        match: EntitySummary | None = None
        for existing in existing_entities:
            if existing.canonical_name.lower() == name.lower():
                match = existing
                break
            if any(a.lower() == name.lower() for a in existing.aliases):
                match = existing
                break

        if match is not None:
            result.append(
                ResolvedEntityInfo(
                    name=name,
                    entity_id=match.entity_id,
                    canonical_name=match.canonical_name,
                    entity_type=match.entity_type,
                    is_new=False,
                )
            )
        else:
            etype = EntityType.concept
            if type_hints and name in type_hints:
                with contextlib.suppress(ValueError):
                    etype = EntityType(type_hints[name])
            new_entity = Entity(canonical_name=name, type=etype)
            result.append(
                ResolvedEntityInfo(
                    name=name,
                    entity_id=new_entity.id,
                    canonical_name=name,
                    entity_type=etype.value,
                    is_new=True,
                )
            )
    return result


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class EntityTrackerAgent(BaseAgent):
    name = "entity_tracker"

    def __init__(self, llm: Any | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> EntityTrackerOutput:
        """Phase 1 path: requires db_conn. Queries and writes directly."""
        assert isinstance(input, EntityTrackerInput)
        assert isinstance(self.db_conn, duckdb.DuckDBPyConnection)

        resolved: dict[str, str] = {}
        created_count = 0

        for name in input.names:
            existing_id = _find_entity_by_name(self.db_conn, name)
            if existing_id is not None:
                resolved[name] = existing_id
                continue

            entity_type = EntityType.concept
            if input.type_hints:
                entity_type = input.type_hints.get(name, EntityType.concept)

            entity = Entity(canonical_name=name, type=entity_type)
            create_entity(self.db_conn, entity)
            resolved[name] = entity.id
            created_count += 1

        return EntityTrackerOutput(resolved=resolved, created_count=created_count)

    async def run_skill(self, input: EntityResolveSkillInput) -> EntityResolveSkillOutput:
        """Phase 2 path: pure, no DB. Used by the A2A server executor."""
        resolved = resolve_entities_pure(
            input.candidate_names,
            input.existing_entities,
            input.type_hints,
        )
        return EntityResolveSkillOutput(resolved=resolved)

    def to_a2a_server(self, url: str) -> Starlette:
        card = build_agent_card(
            name="Entity Tracker",
            description="Resolves candidate entity names against known entities.",
            url=url,
            skill_id="resolve_entities",
            skill_name="Resolve Entities",
            skill_description="Match candidate names to existing entities; create new ones.",
            skill_tags=["entities", "resolution", "deduplication"],
        )
        handler = DefaultRequestHandler(
            agent_executor=_EntityTrackerExecutor(),
            task_store=InMemoryTaskStore(),
            agent_card=card,
        )
        routes: list[Route] = []
        routes.extend(create_agent_card_routes(card))
        routes.extend(create_jsonrpc_routes(handler, "/"))
        return Starlette(routes=routes)


# ---------------------------------------------------------------------------
# A2A executor
# ---------------------------------------------------------------------------


class _EntityTrackerExecutor(AgentExecutor):
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
        skill_input = EntityResolveSkillInput.model_validate(raw)
        resolved = resolve_entities_pure(
            skill_input.candidate_names,
            skill_input.existing_entities,
            skill_input.type_hints,
        )
        output = EntityResolveSkillOutput(resolved=resolved)

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
