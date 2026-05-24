from __future__ import annotations

from fastapi.testclient import TestClient


def test_list_entities_empty(empty_client: TestClient) -> None:
    r = empty_client.get("/api/v1/entities")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["limit"] == 50
    assert body["offset"] == 0


def test_list_entities_seeded(client: TestClient) -> None:
    r = client.get("/api/v1/entities")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    names = {e["canonical_name"] for e in body["items"]}
    assert names == {"GPT-4", "ImageNet"}


def test_list_entities_filter_by_type(client: TestClient) -> None:
    r = client.get("/api/v1/entities?type=model")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["canonical_name"] == "GPT-4"


def test_list_entities_q_substring(client: TestClient) -> None:
    r = client.get("/api/v1/entities?q=imag")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["canonical_name"] == "ImageNet"


def test_list_entities_pagination(client: TestClient) -> None:
    r = client.get("/api/v1/entities?limit=1&offset=0")
    body = r.json()
    assert len(body["items"]) == 1
    assert body["total"] == 2

    r2 = client.get("/api/v1/entities?limit=1&offset=1")
    body2 = r2.json()
    assert len(body2["items"]) == 1
    assert body["items"][0]["id"] != body2["items"][0]["id"]


def test_entity_detail(client: TestClient) -> None:
    listing = client.get("/api/v1/entities?type=model").json()
    gpt_id = listing["items"][0]["id"]

    r = client.get(f"/api/v1/entities/{gpt_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["entity"]["canonical_name"] == "GPT-4"
    assert len(body["claims"]) == 3
    assert body["relationships"] == []


def test_entity_detail_404(client: TestClient) -> None:
    r = client.get("/api/v1/entities/does-not-exist")
    assert r.status_code == 404


def test_list_entities_limit_clamp(client: TestClient) -> None:
    r = client.get("/api/v1/entities?limit=500")
    assert r.status_code == 422
