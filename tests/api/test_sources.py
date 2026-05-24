from __future__ import annotations

from fastapi.testclient import TestClient


def test_list_sources_empty(empty_client: TestClient) -> None:
    r = empty_client.get("/api/v1/sources")
    assert r.status_code == 200
    assert r.json()["total"] == 0


def test_list_sources_with_counts(client: TestClient) -> None:
    r = client.get("/api/v1/sources")
    body = r.json()
    assert body["total"] == 2
    counts = sorted(item["claim_count"] for item in body["items"])
    assert counts == [1, 2]  # one src has 1 claim, the other 2


def test_list_sources_filter(client: TestClient) -> None:
    r = client.get("/api/v1/sources?type=arxiv")
    assert r.json()["total"] == 2
    r2 = client.get("/api/v1/sources?type=github")
    assert r2.json()["total"] == 0


def test_source_detail(client: TestClient) -> None:
    listing = client.get("/api/v1/sources").json()["items"]
    sid = listing[0]["source"]["id"]
    r = client.get(f"/api/v1/sources/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert body["source"]["id"] == sid
    assert isinstance(body["claims"], list)


def test_source_detail_404(client: TestClient) -> None:
    r = client.get("/api/v1/sources/missing")
    assert r.status_code == 404
