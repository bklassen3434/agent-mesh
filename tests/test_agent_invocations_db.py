"""Phase 23a: agent_invocations record DB layer (create / list / roster / graph)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mesh_db.agent_invocations import (
    agent_graph,
    agent_roster,
    create_agent_invocation,
    get_agent_invocation,
    list_agent_invocations,
)
from mesh_db.connection import MeshConnection
from mesh_models.agent_invocation import AgentInvocation


def _inv(
    *,
    run_id: str = "run-1",
    field_id: str = "ai-robotics",
    agent: str = "claim_extractor",
    skill: str = "extract_claims",
    status: str = "ok",
    latency_ms: int | None = 100,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    created_at: datetime | None = None,
) -> AgentInvocation:
    return AgentInvocation(
        run_id=run_id,
        field_id=field_id,
        agent=agent,
        skill=skill,
        status=status,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        created_at=created_at or datetime.now(UTC),
    )


def test_create_and_get_round_trip(tmp_db: MeshConnection) -> None:
    rec = AgentInvocation(
        run_id="run-1",
        field_id="ai-robotics",
        agent="claim_extractor",
        skill="extract_claims",
        traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01",
        trace_id="a" * 32,
        status="ok",
        input_summary={"truncated": False, "preview": "{}"},
        output_summary={"truncated": False, "preview": "{}"},
        memory_block="=== LEARNED HEURISTICS ===\n- (confidence 0.90) be careful",
        applied_heuristic_ids=["h1", "h2"],
        system_prefix_hash="deadbeef",
        model="claude-haiku-4-5",
        latency_ms=123,
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.0012,
    )
    create_agent_invocation(tmp_db, rec)
    got = get_agent_invocation(tmp_db, rec.id)
    assert got is not None
    assert got.run_id == "run-1"
    assert got.agent == "claim_extractor"
    assert got.trace_id == "a" * 32
    assert got.memory_block is not None and "heuristics" in got.memory_block.lower()
    assert got.applied_heuristic_ids == ["h1", "h2"]
    assert got.system_prefix_hash == "deadbeef"
    assert got.model == "claude-haiku-4-5"
    assert got.input_tokens == 100
    assert got.cost_usd == 0.0012


def test_get_missing_returns_none(tmp_db: MeshConnection) -> None:
    assert get_agent_invocation(tmp_db, "nope") is None


def test_list_is_newest_first_and_field_scoped(tmp_db: MeshConnection) -> None:
    now = datetime.now(UTC)
    create_agent_invocation(tmp_db, _inv(skill="a", created_at=now - timedelta(minutes=2)))
    create_agent_invocation(tmp_db, _inv(skill="b", created_at=now - timedelta(minutes=1)))
    # A row in another field must never surface in ai-robotics reads. The seeded
    # store only has ai-robotics, so assert isolation via the filter not matching.
    rows = list_agent_invocations(tmp_db, field_id="ai-robotics")
    assert [r.skill for r in rows] == ["b", "a"]
    assert list_agent_invocations(tmp_db, field_id="no-such-field") == []


def test_list_filters_by_agent_and_run(tmp_db: MeshConnection) -> None:
    create_agent_invocation(tmp_db, _inv(agent="claim_extractor", run_id="run-1"))
    create_agent_invocation(tmp_db, _inv(agent="sota_tracker", run_id="run-1"))
    create_agent_invocation(tmp_db, _inv(agent="claim_extractor", run_id="run-2"))

    by_agent = list_agent_invocations(tmp_db, agent="claim_extractor")
    assert len(by_agent) == 2
    assert {r.agent for r in by_agent} == {"claim_extractor"}

    by_run = list_agent_invocations(tmp_db, run_id="run-1")
    assert len(by_run) == 2
    assert {r.run_id for r in by_run} == {"run-1"}

    both = list_agent_invocations(tmp_db, agent="claim_extractor", run_id="run-2")
    assert len(both) == 1


def test_list_respects_limit(tmp_db: MeshConnection) -> None:
    for i in range(5):
        create_agent_invocation(tmp_db, _inv(skill=f"s{i}"))
    assert len(list_agent_invocations(tmp_db, limit=3)) == 3
    assert list_agent_invocations(tmp_db, limit=0) == []


def test_roster_aggregates(tmp_db: MeshConnection) -> None:
    create_agent_invocation(
        tmp_db, _inv(agent="claim_extractor", status="ok", latency_ms=100,
                     input_tokens=10, output_tokens=5, cost_usd=0.001)
    )
    create_agent_invocation(
        tmp_db, _inv(agent="claim_extractor", status="error", latency_ms=200,
                     input_tokens=20, output_tokens=10, cost_usd=0.002)
    )
    create_agent_invocation(tmp_db, _inv(agent="sota_tracker", status="ok", latency_ms=50))

    roster = {e.agent: e for e in agent_roster(tmp_db)}
    ce = roster["claim_extractor"]
    assert ce.invocations == 2
    assert ce.errors == 1
    assert ce.error_rate == 0.5
    assert ce.avg_latency_ms == 150.0
    assert ce.total_input_tokens == 30
    assert ce.total_output_tokens == 15
    assert abs(ce.total_cost_usd - 0.003) < 1e-9
    assert ce.last_active is not None
    # busiest agent first
    assert agent_roster(tmp_db)[0].agent == "claim_extractor"


def test_roster_last_run_is_most_recent(tmp_db: MeshConnection) -> None:
    now = datetime.now(UTC)
    create_agent_invocation(
        tmp_db, _inv(agent="skeptic", run_id="old", created_at=now - timedelta(hours=1))
    )
    create_agent_invocation(
        tmp_db, _inv(agent="skeptic", run_id="new", created_at=now)
    )
    entry = agent_roster(tmp_db)[0]
    assert entry.last_run_id == "new"


def test_agent_graph_is_coordinator_star(tmp_db: MeshConnection) -> None:
    create_agent_invocation(tmp_db, _inv(agent="claim_extractor"))
    create_agent_invocation(tmp_db, _inv(agent="claim_extractor"))
    create_agent_invocation(tmp_db, _inv(agent="sota_tracker", status="error"))

    graph = agent_graph(tmp_db)
    by_id = {n.id: n for n in graph.nodes}
    assert by_id["coordinator"].role == "coordinator"
    assert by_id["coordinator"].invocation_count == 3
    assert by_id["claim_extractor"].invocation_count == 2
    assert by_id["sota_tracker"].error_rate == 1.0
    # every edge originates at the coordinator hub
    assert {e.source for e in graph.edges} == {"coordinator"}
    ce_edge = next(e for e in graph.edges if e.target == "claim_extractor")
    assert ce_edge.call_count == 2


def test_empty_field_reads(tmp_db: MeshConnection) -> None:
    assert list_agent_invocations(tmp_db) == []
    assert agent_roster(tmp_db) == []
    g = agent_graph(tmp_db)
    assert [n.id for n in g.nodes] == ["coordinator"]
    assert g.edges == []
