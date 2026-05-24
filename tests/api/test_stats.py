from __future__ import annotations

from fastapi.testclient import TestClient


def test_stats_empty(empty_client: TestClient) -> None:
    r = empty_client.get("/api/v1/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["entities"] == 0
    assert body["claims"] == 0
    assert body["beliefs"] == 0
    assert body["sources"] == 0
    assert body["revisions"] == 0
    assert body["pipeline_runs"] == 0
    assert body["last_pipeline_run_at"] is None
    assert body["last_pipeline_run_id"] is None


def test_stats_populated(client: TestClient) -> None:
    r = client.get("/api/v1/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["entities"] == 2
    assert body["claims"] == 3
    assert body["beliefs"] == 1
    assert body["sources"] == 2
    assert body["revisions"] == 1
    assert body["pipeline_runs"] == 1
    assert body["last_pipeline_run_at"] is not None
    assert body["last_pipeline_run_id"] is not None
