"""Phase 23b: the /api/v1/agents* observability endpoints."""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from mesh_db.agent_invocations import create_agent_invocation
from mesh_db.connection import get_connection
from mesh_db.heuristics import create_heuristic
from mesh_models.agent_invocation import AgentInvocation
from mesh_models.heuristic import AgentHeuristic


@pytest.fixture
def agents_client(empty_db_path: object) -> Iterator[TestClient]:
    """A client over a store seeded with agent invocations + one heuristic."""
    conn = get_connection(read_only=False)
    try:
        h = AgentHeuristic(
            agent="claim_extractor",
            skill="extract_claims",
            heuristic="forum scores are self-reported",
            confidence=0.8,
        )
        create_heuristic(conn, h, field_id="ai-robotics")
        create_agent_invocation(
            conn,
            AgentInvocation(
                run_id="run-1", field_id="ai-robotics", agent="claim_extractor",
                skill="extract_claims", status="ok", trace_id="t" * 32,
                latency_ms=120, input_tokens=100, output_tokens=40, cost_usd=0.001,
                applied_heuristic_ids=[h.id], memory_block="=== LEARNED ===",
                input_summary={"truncated": False, "preview": "{}"},
                output_summary={"truncated": False, "preview": "{}"},
            ),
        )
        create_agent_invocation(
            conn,
            AgentInvocation(
                run_id="run-1", field_id="ai-robotics", agent="claim_extractor",
                skill="extract_claims", status="error", error_type="SkillCallError",
                latency_ms=80,
            ),
        )
        create_agent_invocation(
            conn,
            AgentInvocation(
                run_id="run-1", field_id="ai-robotics", agent="sota_tracker",
                skill="update_sota", status="ok", latency_ms=30,
            ),
        )
    finally:
        conn.close()
    from mesh_api.main import create_app

    with TestClient(create_app()) as c:
        yield c


def test_roster(agents_client: TestClient) -> None:
    r = agents_client.get("/api/v1/agents")
    assert r.status_code == 200
    roster = {e["agent"]: e for e in r.json()}
    ce = roster["claim_extractor"]
    assert ce["invocations"] == 2
    assert ce["errors"] == 1
    assert ce["error_rate"] == 0.5
    assert "sota_tracker" in roster


def test_agent_invocations(agents_client: TestClient) -> None:
    r = agents_client.get("/api/v1/agents/claim_extractor/invocations")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    assert {row["agent"] for row in rows} == {"claim_extractor"}


def test_invocation_detail_resolves_heuristics(agents_client: TestClient) -> None:
    listing = agents_client.get("/api/v1/agents/claim_extractor/invocations").json()
    target = next(i for i in listing if i["status"] == "ok")
    r = agents_client.get(f"/api/v1/agents/invocations/{target['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["invocation"]["id"] == target["id"]
    assert len(body["applied_heuristics"]) == 1
    assert "self-reported" in body["applied_heuristics"][0]["heuristic"]


def test_invocation_detail_404(agents_client: TestClient) -> None:
    assert agents_client.get("/api/v1/agents/invocations/nope").status_code == 404


def test_agent_memory(agents_client: TestClient) -> None:
    r = agents_client.get("/api/v1/agents/claim_extractor/memory")
    assert r.status_code == 200
    body = r.json()
    assert body["agent"] == "claim_extractor"
    assert len(body["heuristics"]) == 1
    assert "episodic" in body


def test_agent_graph(agents_client: TestClient) -> None:
    r = agents_client.get("/api/v1/agents/graph")
    assert r.status_code == 200
    body = r.json()
    node_ids = {n["id"] for n in body["nodes"]}
    assert "coordinator" in node_ids
    assert "claim_extractor" in node_ids
    assert {e["source"] for e in body["edges"]} == {"coordinator"}


def test_field_scoping(agents_client: TestClient) -> None:
    r = agents_client.get("/api/v1/agents?field=no-such-field")
    assert r.json() == []


def test_langfuse_url_present_when_configured(
    agents_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANGFUSE_HOST", "https://lf.example.com")
    listing = agents_client.get("/api/v1/agents/claim_extractor/invocations").json()
    target = next(i for i in listing if i["trace_id"])
    body = agents_client.get(f"/api/v1/agents/invocations/{target['id']}").json()
    assert body["langfuse_url"] == f"https://lf.example.com/trace/{target['trace_id']}"
