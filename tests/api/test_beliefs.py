from __future__ import annotations

from fastapi.testclient import TestClient


def test_list_beliefs_empty(empty_client: TestClient) -> None:
    r = empty_client.get("/api/v1/beliefs")
    body = r.json()
    assert body["total"] == 0 and body["items"] == []


def test_list_beliefs_seeded(client: TestClient) -> None:
    r = client.get("/api/v1/beliefs")
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["topic"] == "GPT-4 ImageNet SOTA"


def test_list_beliefs_filter(client: TestClient) -> None:
    r = client.get("/api/v1/beliefs?topic=ImageNet")
    assert r.json()["total"] == 1
    r2 = client.get("/api/v1/beliefs?topic=nonexistent")
    assert r2.json()["total"] == 0


def test_belief_detail_full_shape(client: TestClient) -> None:
    listing = client.get("/api/v1/beliefs").json()
    bid = listing["items"][0]["id"]
    r = client.get(f"/api/v1/beliefs/{bid}")
    assert r.status_code == 200
    body = r.json()

    assert body["belief"]["id"] == bid

    # Two supporting claims, each with source and subject entity hydrated.
    assert len(body["supporting_claims"]) == 2
    for c in body["supporting_claims"]:
        assert c["source"] is not None
        assert c["subject_entity"]["canonical_name"] == "GPT-4"

    # One contradicting claim.
    assert len(body["contradicting_claims"]) == 1

    # One revision with one trigger claim hydrated.
    assert len(body["revisions"]) == 1
    rev = body["revisions"][0]
    assert rev["revision"]["new_statement"] == "GPT-4 achieves 93% on ImageNet."
    assert len(rev["trigger_claims"]) == 1


def test_belief_detail_404(client: TestClient) -> None:
    r = client.get("/api/v1/beliefs/missing")
    assert r.status_code == 404
