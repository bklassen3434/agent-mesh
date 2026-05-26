"""Starlette factory: agent card + healthz + mesh task endpoints.

Each agent server uses ``build_task_app`` to expose:

* ``GET /.well-known/agent-card.json``   — A2A capability discovery
* ``GET /healthz``                       — liveness probe
* ``POST /mesh/tasks/submit``            — enqueue work, return ``task_id``
* ``GET /mesh/tasks/{task_id}``          — poll status / fetch result

Handlers are plain async callables ``(payload: dict) -> dict``. They run in
``asyncio.create_task`` background coroutines and the registry tracks
progress; the ``submit`` request never blocks on the work itself.
"""
from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

from a2a.server.routes import create_agent_card_routes
from a2a.types import AgentCard
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mesh_a2a.task_registry import TaskRegistry

logger = logging.getLogger(__name__)

SkillHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def build_task_app(
    *,
    agent_card: AgentCard,
    skill_handlers: dict[str, SkillHandler],
    agent_name: str,
) -> Starlette:
    """Build the Starlette app for an agent server.

    Args:
        agent_card: AgentCard served at /.well-known/agent-card.json.
        skill_handlers: skill_id -> async callable. Each handler receives the
            JSON payload submitted by the client and returns a JSON-serializable
            result dict.
        agent_name: identifier returned by /healthz (e.g. "arxiv_scout").
    """
    registry = TaskRegistry()

    declared_skills = {s.id for s in agent_card.skills}
    missing = declared_skills - set(skill_handlers.keys())
    if missing:
        raise ValueError(
            f"Agent card declares skills {missing} with no registered handler"
        )

    async def _healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "agent": agent_name})

    async def _submit(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception as exc:
            return JSONResponse({"error": f"invalid JSON body: {exc}"}, status_code=400)

        skill_id = body.get("skill_id")
        payload = body.get("payload", {})
        traceparent = body.get("traceparent")

        if not skill_id or skill_id not in skill_handlers:
            return JSONResponse(
                {"error": f"unknown skill_id: {skill_id!r}"}, status_code=404
            )

        record = await registry.create(
            skill_id,
            metadata={"traceparent": traceparent} if traceparent else None,
        )
        handler = skill_handlers[skill_id]

        async def _run() -> None:
            await registry.mark_running(record.task_id)
            try:
                result = await handler(payload)
                await registry.mark_completed(record.task_id, result)
            except Exception as exc:
                tb = traceback.format_exc()
                logger.warning(
                    "skill_handler_failed",
                    extra={
                        "skill_id": skill_id,
                        "task_id": record.task_id,
                        "error": str(exc),
                        "traceback": tb,
                    },
                )
                await registry.mark_failed(record.task_id, f"{type(exc).__name__}: {exc}")

        bg = asyncio.create_task(_run())
        bg.add_done_callback(lambda _t: None)  # prevent "never awaited" warnings
        return JSONResponse({"task_id": record.task_id}, status_code=202)

    async def _get(request: Request) -> JSONResponse:
        task_id = request.path_params["task_id"]
        rec = await registry.get(task_id)
        if rec is None:
            return JSONResponse({"error": f"task {task_id} not found"}, status_code=404)
        return JSONResponse(rec.to_wire())

    routes: list[Route] = []
    routes.extend(create_agent_card_routes(agent_card))
    routes.extend(
        [
            Route("/healthz", endpoint=_healthz, methods=["GET"]),
            Route("/mesh/tasks/submit", endpoint=_submit, methods=["POST"]),
            Route("/mesh/tasks/{task_id}", endpoint=_get, methods=["GET"]),
        ]
    )

    app = Starlette(routes=routes)
    app.state.task_registry = registry
    return app
