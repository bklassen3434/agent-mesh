"""Tests for /api/v1/briefing.

The endpoint composes DB candidate gathering with an A2A dispatch to the
Personalizer agent. We don't spin up a real Personalizer here — instead
we patch ``MeshA2AClient`` so the test exercises the route's profile
loading, window computation, caching, and HTTP response shape without
needing an LLM.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _fake_briefing_dict() -> dict[str, Any]:
    return {
        "date": "2026-05-25",
        "profile_excerpt": "ops + eval focus",
        "sections": [
            {
                "name": "New Beliefs",
                "description": None,
                "items": [
                    {
                        "item_type": "belief",
                        "item_id": "b1",
                        "relevance_score": 0.9,
                        "rationale": "Directly relevant to your eval focus.",
                    }
                ],
            }
        ],
    }


@pytest.fixture
def profile_at(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Write a profile to a temp path and point MESH_PROFILE_PATH at it."""
    path = tmp_path / "profile.md"
    path.write_text("I care about LLM observability and agent eval.\n")
    monkeypatch.setenv("MESH_PROFILE_PATH", str(path))
    return path


def test_briefing_404_without_profile(
    empty_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MESH_PROFILE_PATH", str(tmp_path / "does-not-exist.md"))
    resp = empty_client.get("/api/v1/briefing")
    assert resp.status_code == 404
    assert "profile" in resp.json()["detail"].lower()


def test_briefing_returns_quiet_day_when_no_candidates(
    empty_client: TestClient, profile_at: Path
) -> None:
    resp = empty_client.get("/api/v1/briefing?date=2026-05-25")
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == "2026-05-25"
    assert body["sections"][0]["name"] == "Quiet day"
    # No LLM call should have been needed.


def test_briefing_dispatches_to_personalizer_when_candidates_exist(
    empty_client: TestClient, profile_at: Path, seeded_db_path: Path  # noqa: ARG001
) -> None:
    """With candidates in the DB, the route should dispatch via MeshA2AClient.

    The seeded fixture inserts beliefs/claims dated 2025-01..02, so we ask
    for that date window.
    """
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.discover = AsyncMock(
        return_value={"personalize_digest": "http://fake-personalizer"}
    )
    fake_client.call_skill_blocking = AsyncMock(return_value=_fake_briefing_dict())

    with patch(
        "mesh_api.routers.briefing.MeshA2AClient", return_value=fake_client
    ):
        # The seeded fixture's belief has last_revised_at "now"; query "today".
        from datetime import UTC, datetime
        today = datetime.now(UTC).date().isoformat()
        resp = empty_client.get(f"/api/v1/briefing?date={today}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sections"][0]["items"][0]["item_id"] == "b1"
    fake_client.call_skill_blocking.assert_awaited_once()


def test_briefing_cached_per_day_and_profile(
    empty_client: TestClient, profile_at: Path
) -> None:
    """Two requests with the same date+profile should only hit the LLM once."""
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.discover = AsyncMock(
        return_value={"personalize_digest": "http://fake-personalizer"}
    )
    fake_client.call_skill_blocking = AsyncMock(return_value=_fake_briefing_dict())

    # Reset module cache to make the assertion deterministic between tests.
    from mesh_api.routers import briefing as briefing_router
    briefing_router._CACHE.clear()

    with patch(
        "mesh_api.routers.briefing.MeshA2AClient", return_value=fake_client
    ):
        # Force a candidate set by writing a belief with today's timestamp.
        # The empty fixture has no rows, so the quiet-day fast-path would hit;
        # to exercise caching of a real briefing, we seed a row directly.
        from datetime import UTC, datetime

        from mesh_db.beliefs import create_belief
        from mesh_db.connection import get_connection
        from mesh_models.belief import Belief

        conn = get_connection(read_only=False)
        try:
            create_belief(
                conn,
                Belief(
                    topic="cache test",
                    statement="something happened today",
                    confidence=0.7,
                    last_revised_at=datetime.now(UTC),
                    revision_count=0,
                ),
            )
        finally:
            conn.close()

        today = datetime.now(UTC).date().isoformat()
        r1 = empty_client.get(f"/api/v1/briefing?date={today}")
        r2 = empty_client.get(f"/api/v1/briefing?date={today}")
    assert r1.status_code == 200 and r2.status_code == 200
    assert fake_client.call_skill_blocking.await_count == 1
    assert r1.json() == r2.json()
