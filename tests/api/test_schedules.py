"""Phase 9 tests for schedule + pipeline-control endpoints.

These run without Postgres (LANGGRAPH_POSTGRES_URL unset) and without a
live scheduler, so they cover the validation + graceful-degradation paths.
Full read/write against Postgres + a running scheduler is exercised in the
docker stack.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _no_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGGRAPH_POSTGRES_URL", raising=False)


def test_list_schedules_503_without_postgres(empty_client: TestClient) -> None:
    r = empty_client.get("/api/v1/schedules")
    assert r.status_code == 503


def test_patch_unknown_job_404(empty_client: TestClient) -> None:
    r = empty_client.patch("/api/v1/schedules/nope", json={"enabled": False})
    assert r.status_code == 404


def test_patch_rejects_bad_interval(empty_client: TestClient) -> None:
    r = empty_client.patch("/api/v1/schedules/pipeline", json={"interval_hours": 5})
    assert r.status_code == 422


def test_patch_requires_a_field(empty_client: TestClient) -> None:
    r = empty_client.patch("/api/v1/schedules/pipeline", json={})
    assert r.status_code == 422


def test_trigger_unknown_job_404(empty_client: TestClient) -> None:
    r = empty_client.post("/api/v1/pipelines/nope/trigger")
    assert r.status_code == 404


def test_scheduler_status_degrades_when_unreachable(
    empty_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No scheduler running on the default URL → endpoint returns [].
    monkeypatch.setenv("SCHEDULER_URL", "http://127.0.0.1:9")
    r = empty_client.get("/api/v1/scheduler/status")
    assert r.status_code == 200
    assert r.json() == []
