"""Phase 8 tests for the LangGraph skill-node wrapper + checkpointer helper."""
from __future__ import annotations

from typing import Any

import pytest
from mesh_a2a.checkpoint import open_checkpointer, postgres_url, thread_config
from mesh_a2a.client import SkillCallError, SkillNotFoundError
from mesh_a2a.node import TaskError, call_skill_node


class _FakeClient:
    """Minimal stand-in exposing only call_skill_blocking."""

    def __init__(
        self,
        responses: dict[str, dict[str, Any]],
        raises: dict[str, Exception] | None = None,
    ):
        self._responses = responses
        self._raises = raises or {}
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    async def call_skill_blocking(
        self,
        skill_id: str,
        payload: dict[str, Any],
        *,
        traceparent: str | None = None,
        poll_interval: float | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.calls.append((skill_id, payload, traceparent))
        if skill_id in self._raises:
            raise self._raises[skill_id]
        return self._responses[skill_id]


@pytest.mark.asyncio
async def test_call_skill_node_success_returns_result_no_error() -> None:
    client = _FakeClient({"scout_arxiv": {"papers": [1, 2]}})
    result, error = await call_skill_node(
        client,  # type: ignore[arg-type]
        "scout_arxiv",
        {"x": 1},
        traceparent="00-abc-def-01",
    )
    assert result == {"papers": [1, 2]}
    assert error is None
    # traceparent is forwarded verbatim — distributed tracing must survive.
    assert client.calls[0][2] == "00-abc-def-01"


@pytest.mark.asyncio
async def test_call_skill_node_skill_call_error_is_captured_not_raised() -> None:
    client = _FakeClient({}, raises={"extract_claims": SkillCallError("model 503")})
    result, error = await call_skill_node(
        client,  # type: ignore[arg-type]
        "extract_claims",
        {},
        context={"arxiv_id": "2401.1"},
    )
    assert result is None
    assert isinstance(error, TaskError)
    assert error.skill_id == "extract_claims"
    assert error.error_type == "SkillCallError"
    assert "503" in error.error_message
    assert error.context == {"arxiv_id": "2401.1"}


@pytest.mark.asyncio
async def test_call_skill_node_skill_not_found_is_captured() -> None:
    client = _FakeClient({}, raises={"missing": SkillNotFoundError("no agent")})
    result, error = await call_skill_node(client, "missing", {})  # type: ignore[arg-type]
    assert result is None
    assert error is not None
    assert error.error_type == "SkillNotFoundError"


@pytest.mark.asyncio
async def test_call_skill_node_unexpected_error_is_captured() -> None:
    client = _FakeClient({}, raises={"boom": ValueError("kaboom")})
    result, error = await call_skill_node(client, "boom", {})  # type: ignore[arg-type]
    assert result is None
    assert error is not None
    assert error.error_type == "ValueError"


def test_postgres_url_blank_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGGRAPH_POSTGRES_URL", raising=False)
    assert postgres_url() is None
    monkeypatch.setenv("LANGGRAPH_POSTGRES_URL", "   ")
    assert postgres_url() is None
    monkeypatch.setenv("LANGGRAPH_POSTGRES_URL", "postgresql://x")
    assert postgres_url() == "postgresql://x"


def test_thread_config_shape() -> None:
    assert thread_config("run-123") == {"configurable": {"thread_id": "run-123"}}


@pytest.mark.asyncio
async def test_open_checkpointer_falls_back_to_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    monkeypatch.delenv("LANGGRAPH_POSTGRES_URL", raising=False)
    async with open_checkpointer() as saver:
        assert isinstance(saver, InMemorySaver)
