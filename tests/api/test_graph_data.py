"""Phase 9 tests for the pre-aggregated /api/v1/graph/data endpoint."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_graph_data_empty_db(empty_client: TestClient) -> None:
    r = empty_client.get("/api/v1/graph/data")
    assert r.status_code == 200
    body = r.json()
    assert body == {"nodes": [], "edges": [], "total_entities": 0}


def test_graph_data_shape(client: TestClient) -> None:
    r = client.get("/api/v1/graph/data")
    assert r.status_code == 200
    body = r.json()
    assert body["total_entities"] >= 2
    labels = {n["label"] for n in body["nodes"]}
    assert "GPT-4" in labels
    for n in body["nodes"]:
        assert {"id", "label", "type", "belief_count", "last_claim_at"}.issubset(n.keys())
        assert isinstance(n["belief_count"], int)
    for e in body["edges"]:
        assert {"source", "target", "relationship_type", "claim_count"}.issubset(e.keys())


def test_graph_data_edges_within_node_set(client: TestClient) -> None:
    r = client.get("/api/v1/graph/data")
    body = r.json()
    node_ids = {n["id"] for n in body["nodes"]}
    for e in body["edges"]:
        assert e["source"] in node_ids
        assert e["target"] in node_ids
