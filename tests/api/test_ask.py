"""Tests for POST /api/v1/ask.

The endpoint dispatches the ResearchQA agent over A2A. We patch
``MeshA2AClient`` so the test exercises the route's request validation,
field scoping, response shape, and graceful degradation without an LLM.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient


def _fake_answer() -> dict[str, Any]:
    return {
        "answer_markdown": "Atlas leads bipedal locomotion [belief:b1].",
        "citations": [{"kind": "belief", "id": "b1", "quote": "leads"}],
        "coverage": "well_supported",
        "caveats": [],
    }


def _fake_client(discovered: dict[str, str], result: dict[str, Any]) -> MagicMock:
    c = MagicMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=None)
    c.discover = AsyncMock(return_value=discovered)
    c.call_skill_blocking = AsyncMock(return_value=result)
    return c


def test_ask_dispatches_and_returns_cited_answer(empty_client: TestClient) -> None:
    fake = _fake_client({"research_qa": "http://fake-qa"}, _fake_answer())
    with patch("mesh_api.routers.ask.MeshA2AClient", return_value=fake):
        resp = empty_client.post("/api/v1/ask", json={"question": "Atlas locomotion?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["coverage"] == "well_supported"
    assert body["citations"][0]["id"] == "b1"
    # field defaults to ai-robotics
    payload = fake.call_skill_blocking.await_args.args[1]
    assert payload == {"question": "Atlas locomotion?", "field_id": "ai-robotics"}


def test_ask_passes_field_scope(empty_client: TestClient) -> None:
    fake = _fake_client({"research_qa": "http://fake-qa"}, _fake_answer())
    with patch("mesh_api.routers.ask.MeshA2AClient", return_value=fake):
        resp = empty_client.post(
            "/api/v1/ask?field=agribusiness", json={"question": "harvest timing?"}
        )
    assert resp.status_code == 200
    payload = fake.call_skill_blocking.await_args.args[1]
    assert payload["field_id"] == "agribusiness"


def test_ask_rejects_empty_question(empty_client: TestClient) -> None:
    resp = empty_client.post("/api/v1/ask", json={"question": "   "})
    assert resp.status_code == 422


def test_ask_degrades_when_agent_unreachable(empty_client: TestClient) -> None:
    # No skill discovered → clean uncovered answer, not a 500.
    fake = _fake_client({}, _fake_answer())
    with patch("mesh_api.routers.ask.MeshA2AClient", return_value=fake):
        resp = empty_client.post("/api/v1/ask", json={"question": "anything?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["coverage"] == "uncovered"
    assert body["caveats"]
    fake.call_skill_blocking.assert_not_awaited()


def test_ask_timeout_is_504(empty_client: TestClient) -> None:
    from mesh_a2a.client import TaskTimeoutError

    fake = _fake_client({"research_qa": "http://fake-qa"}, _fake_answer())
    fake.call_skill_blocking = AsyncMock(side_effect=TaskTimeoutError("too slow"))
    with patch("mesh_api.routers.ask.MeshA2AClient", return_value=fake):
        resp = empty_client.post("/api/v1/ask", json={"question": "anything?"})
    assert resp.status_code == 504
