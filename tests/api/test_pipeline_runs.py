from __future__ import annotations

from fastapi.testclient import TestClient


def test_pipeline_runs_empty(empty_client: TestClient) -> None:
    r = empty_client.get("/api/v1/pipeline-runs")
    assert r.status_code == 200
    assert r.json() == []


def test_pipeline_runs_seeded(client: TestClient) -> None:
    r = client.get("/api/v1/pipeline-runs")
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) == 1
    assert runs[0]["claims_inserted"] == 3
    assert runs[0]["entities_created"] == 2


def test_pipeline_runs_limit_clamp(client: TestClient) -> None:
    r = client.get("/api/v1/pipeline-runs?limit=500")
    assert r.status_code == 422  # Query(le=200) enforces upper bound
