"""A2A client: discovery, skill dispatch, traceparent injection, retry."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx
from a2a.client import A2ACardResolver, ClientFactory
from a2a.client.client import Client, ClientConfig
from a2a.client.interceptors import AfterArgs, BeforeArgs, ClientCallInterceptor
from a2a.helpers.proto_helpers import new_data_part
from a2a.types import Message, Role, SendMessageRequest, StreamResponse, Task
from google.protobuf.json_format import MessageToDict

from mesh_a2a.tracing import TRACEPARENT_KEY, new_traceparent

logger = logging.getLogger(__name__)


class _TraceparentInterceptor(ClientCallInterceptor):
    """Injects the current traceparent into every outbound SendMessageRequest."""

    def __init__(self, traceparent: str) -> None:
        self._tp = traceparent

    async def before(self, args: BeforeArgs) -> None:
        if isinstance(args.input, SendMessageRequest):
            args.input.metadata[TRACEPARENT_KEY] = self._tp

    async def after(self, args: AfterArgs) -> None:
        pass


class SkillNotFoundError(RuntimeError):
    """Raised when the coordinator has no agent registered for a skill_id."""


class SkillCallError(RuntimeError):
    """Raised when a skill call returns an error or unexpected response."""


def _extract_result(response: StreamResponse) -> dict[str, Any]:
    """Extract the first data artifact from a StreamResponse."""
    task: Task | None = None
    if response.HasField("task"):
        task = response.task
    elif response.HasField("message"):
        # Agent returned a direct message — extract data part if present
        for part in response.message.parts:
            if part.HasField("data"):
                return dict(MessageToDict(part.data))
        raise SkillCallError("Agent returned a message with no data part")
    else:
        raise SkillCallError("Unexpected StreamResponse shape (not task or message)")

    if task is None:
        raise SkillCallError("No task in response")

    for artifact in task.artifacts:
        for part in artifact.parts:
            if part.HasField("data"):
                return dict(MessageToDict(part.data))
        for part in artifact.parts:
            if part.HasField("text"):
                try:
                    return dict(json.loads(part.text))
                except json.JSONDecodeError as exc:
                    raise SkillCallError(f"Non-JSON artifact text: {part.text}") from exc

    raise SkillCallError("Agent task completed with no usable artifacts")


class MeshA2AClient:
    """Coordinator-side A2A client.

    Usage::

        async with MeshA2AClient() as client:
            await client.discover(["http://arxiv-scout:8001", ...])
            result = await client.call_skill("scout_arxiv", {...}, traceparent=tp)
    """

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=120.0)
        self._factory = ClientFactory(ClientConfig(streaming=False))
        # skill_id -> (base_url, a2a Client)
        self._registry: dict[str, tuple[str, Client]] = {}

    async def __aenter__(self) -> MeshA2AClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def discover(self, base_urls: list[str]) -> dict[str, str]:
        """Fetch agent cards from base_urls; build skill_id -> url mapping.

        Returns a dict of discovered skill_id -> base_url for logging.
        """
        discovered: dict[str, str] = {}
        for url in base_urls:
            try:
                resolver = A2ACardResolver(self._http, url)
                card = await resolver.get_agent_card()
                client = self._factory.create(card)
                for skill in card.skills:
                    self._registry[skill.id] = (url, client)
                    discovered[skill.id] = url
                    logger.info("discovered_skill", extra={"skill_id": skill.id, "url": url})
            except Exception as exc:
                logger.warning(
                    "agent_discovery_failed",
                    extra={"url": url, "error": str(exc)},
                )
        return discovered

    async def call_skill(
        self,
        skill_id: str,
        payload: dict[str, Any],
        *,
        traceparent: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch a synchronous skill call; return the result dict.

        Raises SkillNotFoundError if no agent is registered for skill_id.
        Raises SkillCallError on transport or protocol errors.
        """
        if skill_id not in self._registry:
            raise SkillNotFoundError(f"No agent registered for skill '{skill_id}'")

        _url, client = self._registry[skill_id]
        tp = traceparent or new_traceparent()

        await client.add_interceptor(_TraceparentInterceptor(tp))

        msg = Message(
            role=Role.ROLE_USER,
            parts=[new_data_part(payload)],
            message_id=str(uuid.uuid4()),
        )
        request = SendMessageRequest(message=msg)

        last_response: StreamResponse | None = None
        async for resp in client.send_message(request):
            last_response = resp

        if last_response is None:
            raise SkillCallError(f"No response received from skill '{skill_id}'")

        return _extract_result(last_response)

    def skill_map(self) -> dict[str, str]:
        """Return the current skill_id -> base_url map (for CLI / logging)."""
        return {sid: url for sid, (url, _) in self._registry.items()}
