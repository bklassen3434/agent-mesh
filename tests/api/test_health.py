from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db_present"] is True


def test_healthz_on_empty_db(empty_client: TestClient) -> None:
    r = empty_client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
