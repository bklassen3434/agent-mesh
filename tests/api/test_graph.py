"""Phase 7b.5 tests for the /api/v1/graph endpoint."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_graph_empty_db(empty_client: TestClient) -> None:
    r = empty_client.get("/api/v1/graph")
    assert r.status_code == 200
    body = r.json()
    assert body == {"nodes": [], "edges": []}


def test_graph_returns_seeded_entities_and_relationships(client: TestClient) -> None:
    r = client.get("/api/v1/graph")
    assert r.status_code == 200
    body = r.json()
    # Seeded fixture has GPT-4 + ImageNet → at least 2 nodes
    assert len(body["nodes"]) >= 2
    labels = {n["label"] for n in body["nodes"]}
    assert "GPT-4" in labels
    assert "ImageNet" in labels
    # Each node has id, label, type
    for n in body["nodes"]:
        assert {"id", "label", "type"}.issubset(n.keys())


def test_graph_max_nodes_bounds_result(client: TestClient) -> None:
    r = client.get("/api/v1/graph?max_nodes=1")
    assert r.status_code == 200
    assert len(r.json()["nodes"]) == 1


def test_graph_drops_dangling_edges(client: TestClient) -> None:
    """Edges referencing entities outside the bounded node set are dropped."""
    r = client.get("/api/v1/graph?max_nodes=1")
    body = r.json()
    node_ids = {n["id"] for n in body["nodes"]}
    for e in body["edges"]:
        assert e["source"] in node_ids
        assert e["target"] in node_ids
