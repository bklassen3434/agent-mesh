"""A2A client: discovery + task-based skill dispatch.

Phase 5a moves the wire protocol from sync ``message/send`` (which blocked
until the agent finished) to a task-based submit-then-poll pattern. The
public surface preserves the appearance of a sync call via
``call_skill_blocking``; internally every dispatch submits a task and
polls until completion.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx
from a2a.client import A2ACardResolver

from mesh_a2a.tracing import new_traceparent

logger = logging.getLogger(__name__)


# ── env knobs ──────────────────────────────────────────────────────────────


def _default_poll_interval() -> float:
    return float(os.environ.get("MESH_TASK_POLL_INTERVAL_SECONDS", "0.5"))


def _default_timeout(skill_id: str) -> float:
    """Per-skill timeout via env, falling back to a global default.

    Resolution order:
      1. MESH_TASK_TIMEOUT_<SKILL_UPPERCASED>
      2. MESH_TASK_TIMEOUT_DEFAULT
      3. MESH_LLM_SKILL_TIMEOUT   (legacy compat from Phase 4)
      4. 120.0 seconds
    """
    specific = os.environ.get(f"MESH_TASK_TIMEOUT_{skill_id.upper()}")
    if specific:
        return float(specific)
    fallback = os.environ.get("MESH_TASK_TIMEOUT_DEFAULT") or os.environ.get(
        "MESH_LLM_SKILL_TIMEOUT"
    )
    if fallback:
        return float(fallback)
    return 120.0


# ── errors ─────────────────────────────────────────────────────────────────


class SkillNotFoundError(RuntimeError):
    """Raised when no agent has been discovered for a skill_id."""


class SkillCallError(RuntimeError):
    """Raised when the task fails on the agent side or transport errors out."""


class TaskTimeoutError(SkillCallError):
    """Raised when call_skill_blocking exhausts its polling budget."""


class MeshA2AClient:
    """Coordinator-side A2A client.

    Usage::

        async with MeshA2AClient() as client:
            await client.discover(["http://arxiv-scout:8001", ...])
            result = await client.call_skill_blocking(
                "scout_arxiv", {...}, traceparent=tp
            )
    """

    def __init__(self) -> None:
        # Submit/poll requests are short; only the task itself may be long.
        # 30s is generous for either; the poll loop enforces real timeouts.
        self._http = httpx.AsyncClient(timeout=30.0)
        self._registry: dict[str, str] = {}  # skill_id -> agent base URL

    async def __aenter__(self) -> MeshA2AClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── discovery ──────────────────────────────────────────────────────────

    async def discover(self, base_urls: list[str]) -> dict[str, str]:
        """Fetch agent cards from base_urls; build skill_id -> url mapping.

        Returns the discovered skill_id -> base_url dict for logging.
        """
        discovered: dict[str, str] = {}
        for url in base_urls:
            try:
                resolver = A2ACardResolver(self._http, url)
                card = await resolver.get_agent_card()
                for skill in card.skills:
                    self._registry[skill.id] = url
                    discovered[skill.id] = url
                    logger.info(
                        "discovered_skill", extra={"skill_id": skill.id, "url": url}
                    )
            except Exception as exc:
                logger.warning(
                    "agent_discovery_failed",
                    extra={"url": url, "error": str(exc)},
                )
        return discovered

    def skill_map(self) -> dict[str, str]:
        """Return current skill_id -> base_url map (for CLI / logging)."""
        return dict(self._registry)

    # ── task-based dispatch ────────────────────────────────────────────────

    async def submit_task(
        self,
        skill_id: str,
        payload: dict[str, Any],
        *,
        traceparent: str | None = None,
    ) -> tuple[str, str]:
        """Submit work for ``skill_id``. Returns ``(task_id, agent_url)``."""
        if skill_id not in self._registry:
            raise SkillNotFoundError(f"No agent registered for skill '{skill_id}'")

        url = self._registry[skill_id]
        body = {
            "skill_id": skill_id,
            "payload": payload,
            "traceparent": traceparent or new_traceparent(),
        }
        try:
            resp = await self._http.post(f"{url}/mesh/tasks/submit", json=body)
        except httpx.HTTPError as exc:
            raise SkillCallError(f"transport error submitting '{skill_id}': {exc}") from exc
        if resp.status_code != 202:
            raise SkillCallError(
                f"submit '{skill_id}' returned {resp.status_code}: {resp.text}"
            )
        task_id = resp.json().get("task_id")
        if not task_id:
            raise SkillCallError(f"submit '{skill_id}' returned no task_id")
        return task_id, url

    async def get_task(self, task_id: str, agent_url: str) -> dict[str, Any]:
        """Poll a single task. Returns the wire dict (status, result, error, ...)."""
        try:
            resp = await self._http.get(f"{agent_url}/mesh/tasks/{task_id}")
        except httpx.HTTPError as exc:
            raise SkillCallError(f"transport error polling {task_id}: {exc}") from exc
        if resp.status_code == 404:
            raise SkillCallError(f"task {task_id} not found on {agent_url}")
        if resp.status_code != 200:
            raise SkillCallError(
                f"poll {task_id} returned {resp.status_code}: {resp.text}"
            )
        return dict(resp.json())

    async def call_skill_blocking(
        self,
        skill_id: str,
        payload: dict[str, Any],
        *,
        traceparent: str | None = None,
        poll_interval: float | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Submit a task and poll until it completes, fails, or times out.

        Preserves the *appearance* of a synchronous call; the wire protocol
        is fully task-based underneath. Per-skill timeouts come from env
        (``MESH_TASK_TIMEOUT_<SKILL_UPPERCASED>``); poll interval defaults
        to ``MESH_TASK_POLL_INTERVAL_SECONDS`` or 0.5s.
        """
        interval = poll_interval if poll_interval is not None else _default_poll_interval()
        deadline = timeout if timeout is not None else _default_timeout(skill_id)

        task_id, url = await self.submit_task(
            skill_id, payload, traceparent=traceparent
        )
        start = time.monotonic()
        while True:
            record = await self.get_task(task_id, url)
            status = record.get("status")
            if status == "completed":
                result = record.get("result")
                if result is None:
                    raise SkillCallError(
                        f"task {task_id} reported completed with no result"
                    )
                return dict(result)
            if status == "failed":
                err = record.get("error") or "unknown error"
                raise SkillCallError(f"skill '{skill_id}' failed: {err}")
            if time.monotonic() - start > deadline:
                raise TaskTimeoutError(
                    f"skill '{skill_id}' task {task_id} did not complete within "
                    f"{deadline:.1f}s (last status: {status})"
                )
            await asyncio.sleep(interval)

    # ── backward compat ────────────────────────────────────────────────────

    async def call_skill(
        self,
        skill_id: str,
        payload: dict[str, Any],
        *,
        traceparent: str | None = None,
    ) -> dict[str, Any]:
        """Backward-compatible alias for ``call_skill_blocking``.

        Phase 5a kept this surface so existing tests + callers continue to
        work; new code should call ``call_skill_blocking`` directly so
        per-call timeouts and poll intervals can be passed explicitly.
        """
        return await self.call_skill_blocking(skill_id, payload, traceparent=traceparent)
