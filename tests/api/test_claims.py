from __future__ import annotations

from fastapi.testclient import TestClient


def test_list_claims_empty(empty_client: TestClient) -> None:
    r = empty_client.get("/api/v1/claims")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == [] and body["total"] == 0


def test_list_claims_seeded(client: TestClient) -> None:
    r = client.get("/api/v1/claims")
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3


def test_list_claims_filter_by_predicate(client: TestClient) -> None:
    r = client.get("/api/v1/claims?predicate=achieves_score")
    body = r.json()
    assert body["total"] == 3
    r2 = client.get("/api/v1/claims?predicate=does_not_exist")
    assert r2.json()["total"] == 0


def test_list_claims_filter_by_source(client: TestClient) -> None:
    sources = client.get("/api/v1/sources").json()["items"]
    first = sources[0]["source"]["id"]
    r = client.get(f"/api/v1/claims?source_id={first}")
    assert r.json()["total"] >= 1


def test_list_claims_pagination(client: TestClient) -> None:
    r = client.get("/api/v1/claims?limit=2&offset=0")
    p1 = r.json()
    r2 = client.get("/api/v1/claims?limit=2&offset=2")
    p2 = r2.json()
    assert len(p1["items"]) == 2 and len(p2["items"]) == 1
    assert {c["id"] for c in p1["items"]}.isdisjoint({c["id"] for c in p2["items"]})


def test_claim_detail(client: TestClient) -> None:
    listing = client.get("/api/v1/claims").json()
    cid = listing["items"][0]["id"]
    r = client.get(f"/api/v1/claims/{cid}")
    assert r.status_code == 200
    body = r.json()
    assert body["claim"]["id"] == cid
    assert body["source"] is not None
    assert body["subject_entity"]["canonical_name"] == "GPT-4"


def test_claim_detail_404(client: TestClient) -> None:
    r = client.get("/api/v1/claims/nope")
    assert r.status_code == 404
