"""Phase 8 tests for the LangGraph coordinator graph.

Exercises the graph end-to-end with a fake A2A client (no network, no LLM)
and an in-memory checkpointer. Focus: the conditional edges fire correctly
(zero-claim runs skip entity/sota tracking) and the DB ends up in the same
shape the imperative coordinator produced.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from mesh_db.beliefs import list_beliefs
from mesh_db.claims import list_claims
from mesh_db.connection import get_connection
from mesh_db.entities import list_entities
from mesh_db.pipeline_runs import list_pipeline_runs
from mesh_db.sources import list_sources


def _paper(arxiv_id: str, content_hash: str) -> dict[str, Any]:
    return {
        "source": {
            "id": str(uuid.uuid4()),
            "type": "arxiv",
            "url": f"https://arxiv.org/abs/{arxiv_id}",
            "published_at": "2024-01-15T00:00:00+00:00",
            "raw_content_hash": content_hash,
        },
        "title": f"Paper {arxiv_id}",
        "abstract": "TestModel-7B achieves 87.5% on MMLU.",
        "arxiv_id": arxiv_id,
    }


class _FakeA2AClient:
    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self._map = {sid: f"http://fake/{sid}" for sid in responses}
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    async def __aenter__(self) -> _FakeA2AClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def discover(self, base_urls: list[str]) -> dict[str, str]:
        return dict(self._map)

    def skill_map(self) -> dict[str, str]:
        return dict(self._map)

    async def call_skill_blocking(
        self,
        skill_id: str,
        payload: dict[str, Any],
        *,
        traceparent: str | None = None,
        poll_interval: float | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.calls.append((skill_id, payload, traceparent))
        resp = self._responses[skill_id]
        out: dict[str, Any] = resp(payload) if callable(resp) else resp
        return out


class _HashEmbedder:
    """Deterministic, model-free embedder for tests: distinct texts map to
    near-orthogonal unit vectors (so unrelated entities don't falsely dedup),
    identical texts to identical vectors. Keeps CI from downloading a model."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            digest = hashlib.sha256(t.encode()).digest()
            raw = (digest * (384 // len(digest) + 1))[:384]
            vals = [b / 255.0 - 0.5 for b in raw]
            norm = math.sqrt(sum(v * v for v in vals)) or 1.0
            out.append([v / norm for v in vals])
        return out


def _run(db: str, responses: dict[str, Any]) -> _FakeA2AClient:
    from mesh_pipeline.coordinator import run_pipeline

    fake = _FakeA2AClient(responses)
    with (
        patch("mesh_pipeline.coordinator.MeshA2AClient", return_value=fake),
        patch(
            "mesh_pipeline.coordinator._make_resolution_deps",
            return_value=(_HashEmbedder(), None),
        ),
    ):
        asyncio.run(
            run_pipeline(categories=["cs.AI"], max_papers=5, since=None, db_path=db)
        )
    return fake


_RESOLVE_RESP = {
    "resolved": [
        {
            "name": "TestModel-7B",
            "entity_id": str(uuid.uuid4()),
            "canonical_name": "TestModel-7B",
            "entity_type": "model",
            "is_new": True,
        }
    ]
}

_CLAIM = {
    "predicate": "achieves_score",
    "subject_name": "TestModel-7B",
    "object": {"score": 87.5, "benchmark": "MMLU", "metric": "accuracy"},
    "raw_excerpt": "TestModel-7B achieves 87.5% on MMLU",
    "confidence": 0.9,
}


def _full_responses() -> dict[str, Any]:
    return {
        "scout_arxiv": {"papers": [_paper("2401.0001", "hash-1")]},
        "extract_claims": {"claims": [_CLAIM], "latency_ms": 120},
        "resolve_entities": _RESOLVE_RESP,
        "update_sota": {
            "belief_updates": [
                {
                    "is_new_belief": True,
                    "topic": "sota:MMLU",
                    "new_statement": "TestModel-7B achieves 87.5% on MMLU",
                    "supporting_claim_ids": [],
                    "new_confidence": 0.8,
                    "existing_belief_id": None,
                    "rationale": "new SOTA",
                }
            ]
        },
    }


def test_happy_path_inserts_source_claim_entity_belief(tmp_path: Path) -> None:
    db = str(tmp_path / "coord.db")
    conn = get_connection(db)
    conn.close()

    _run(db, _full_responses())

    conn = get_connection(db)
    assert len(list_sources(conn, limit=50)) == 1
    claims = list_claims(conn, limit=50)
    assert len(claims) == 1
    assert any(e.canonical_name == "TestModel-7B" for e in list_entities(conn, limit=50))
    beliefs = list_beliefs(conn, limit=50)
    assert len(beliefs) == 1 and beliefs[0].topic == "sota:MMLU"
    runs = list_pipeline_runs(conn, limit=5, run_type="ingest")
    assert len(runs) == 1
    assert runs[0].claims_inserted == 1
    assert runs[0].beliefs_created == 1
    assert runs[0].finished_at is not None
    conn.close()


_CAP_CLAIM_A = {
    "predicate": "has_capability",
    "subject_name": "TestModel-7B",
    "object": {"capability": "handles 1M-token context"},
    "raw_excerpt": "handles 1M-token context with linear-time inference",
    "confidence": 0.9,
}
_CAP_CLAIM_B = {
    "predicate": "has_capability",
    "subject_name": "TestModel-7B",
    "object": {"capability": "runs on a single GPU"},
    "raw_excerpt": "runs on a single consumer GPU",
    "confidence": 0.9,
}


def _capability_responses(claims: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "scout_arxiv": {"papers": [_paper("2401.0003", "hash-cap")]},
        "extract_claims": {"claims": claims, "latency_ms": 90},
        "resolve_entities": _RESOLVE_RESP,
        # No score claims → the SOTA handler emits nothing; capability synthesis
        # is coordinator-side and does not use this skill.
        "update_sota": {"belief_updates": []},
    }


def _resolve_all(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve every candidate name as a new model entity (deterministic id),
    so relational targets like `compared_to` become real nodes for edges."""
    return {
        "resolved": [
            {
                "name": name,
                "entity_id": str(uuid.uuid5(uuid.NAMESPACE_URL, name)),
                "canonical_name": name,
                "entity_type": "model",
                "is_new": True,
            }
            for name in payload.get("candidate_names", [])
        ]
    }


_COMPARISON_CLAIM = {
    "predicate": "outperforms",
    "subject_name": "TestModel-7B",
    "object": {"compared_to": "GPT-3", "on": "MMLU"},
    "raw_excerpt": "TestModel-7B outperforms GPT-3 on MMLU",
    "confidence": 0.85,
}


def test_relational_claim_produces_claim_grounded_edge(tmp_path: Path) -> None:
    db = str(tmp_path / "coord.db")
    conn = get_connection(db)
    conn.close()

    responses = {
        "scout_arxiv": {"papers": [_paper("2401.0004", "hash-edge")]},
        "extract_claims": {"claims": [_COMPARISON_CLAIM], "latency_ms": 80},
        "resolve_entities": _resolve_all,
        "update_sota": {"belief_updates": []},
    }
    _run(db, responses)

    conn = get_connection(db)
    from mesh_db.relationships import list_relationships

    edges = list_relationships(conn, limit=50)
    assert len(edges) == 1
    edge = edges[0]
    assert edge.type == "outperforms"
    # Edge is claim-grounded: it links the asserting claim.
    claims = list_claims(conn, limit=50)
    assert len(claims) == 1
    assert edge.evidence_claim_ids == [claims[0].id]
    # Both endpoints are real, distinct entity nodes.
    names = {e.canonical_name for e in list_entities(conn, limit=50)}
    assert {"TestModel-7B", "GPT-3"} <= names
    assert edge.from_entity_id != edge.to_entity_id
    conn.close()


def test_capability_claims_produce_entity_anchored_belief(tmp_path: Path) -> None:
    db = str(tmp_path / "coord.db")
    conn = get_connection(db)
    conn.close()

    # Two capability claims about the same resolved entity must converge on ONE
    # entity-anchored belief carrying both claims as provenance.
    _run(db, _capability_responses([_CAP_CLAIM_A, _CAP_CLAIM_B]))

    conn = get_connection(db)
    beliefs = list_beliefs(conn, limit=50)
    cap_beliefs = [b for b in beliefs if b.topic.startswith("capability:")]
    assert len(cap_beliefs) == 1
    belief = cap_beliefs[0]
    assert belief.statement.startswith("TestModel-7B:")
    assert "1M-token context" in belief.statement
    assert "single GPU" in belief.statement
    assert len(belief.supporting_claim_ids) == 2
    # 14d: confidence is computed from evidence signals, not the hardcoded 0.5.
    # Two supporting claims, no attacks → above base.
    assert belief.confidence > 0.5
    runs = list_pipeline_runs(conn, limit=5, run_type="ingest")
    assert runs[0].beliefs_created == 1
    conn.close()


def test_records_one_agent_invocation_per_dispatch(tmp_path: Path) -> None:
    """Phase 23a: every coordinator skill dispatch is captured as an
    AgentInvocation — field-scoped, with status/trace/latency/input summary."""
    db = str(tmp_path / "coord.db")
    conn = get_connection(db)
    conn.close()

    _run(db, _full_responses())

    from mesh_db.agent_invocations import (
        agent_graph,
        agent_roster,
        list_agent_invocations,
    )

    conn = get_connection(db)
    invs = list_agent_invocations(conn, field_id="ai-robotics", limit=100)
    by_skill = {i.skill for i in invs}
    # the happy path dispatches all four skills exactly once
    assert {"scout_arxiv", "extract_claims", "resolve_entities", "update_sota"} <= by_skill
    # skill→agent mapping (derived for scouts, table for the rest)
    by_agent = {i.skill: i.agent for i in invs}
    assert by_agent["extract_claims"] == "claim_extractor"
    assert by_agent["resolve_entities"] == "entity_tracker"
    assert by_agent["update_sota"] == "sota_tracker"
    assert by_agent["scout_arxiv"] == "arxiv_scout"
    for i in invs:
        assert i.status == "ok"
        assert i.trace_id is not None  # extracted from the run traceparent
        assert i.latency_ms is not None
        assert i.input_summary is not None
    # all share the one run id, which is the recorded pipeline run
    runs = list_pipeline_runs(conn, limit=1, run_type="ingest")
    assert {i.run_id for i in invs} == {runs[0].id}

    # roster + graph derive from the rows
    roster = {e.agent for e in agent_roster(conn, field_id="ai-robotics")}
    assert "claim_extractor" in roster
    graph = agent_graph(conn, field_id="ai-robotics")
    assert any(n.id == "coordinator" for n in graph.nodes)
    assert {e.source for e in graph.edges} == {"coordinator"}
    conn.close()


def test_failed_dispatch_records_error_invocation(tmp_path: Path) -> None:
    """A skill that errors still records an invocation, with status=error — and
    the run still completes (capture never aborts a run)."""
    db = str(tmp_path / "coord.db")
    conn = get_connection(db)
    conn.close()

    def _boom(_payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("extractor exploded")

    responses = {
        "scout_arxiv": {"papers": [_paper("2401.0009", "hash-err")]},
        "extract_claims": _boom,
        "resolve_entities": _RESOLVE_RESP,
        "update_sota": {"belief_updates": []},
    }
    _run(db, responses)

    from mesh_db.agent_invocations import list_agent_invocations

    conn = get_connection(db)
    invs = list_agent_invocations(conn, field_id="ai-robotics", limit=100)
    extract = [i for i in invs if i.skill == "extract_claims"]
    assert len(extract) == 1
    assert extract[0].status == "error"
    assert extract[0].error_type is not None
    # the run still finalized despite the failed dispatch
    runs = list_pipeline_runs(conn, limit=1, run_type="ingest")
    assert len(runs) == 1 and runs[0].finished_at is not None
    conn.close()


def test_zero_claims_skips_entity_and_sota(tmp_path: Path) -> None:
    """Conditional edge: extract→finalize when no claims, so no entity
    resolution and no sota tracking happen."""
    db = str(tmp_path / "coord.db")
    conn = get_connection(db)
    conn.close()

    responses = {
        "scout_arxiv": {"papers": [_paper("2401.0002", "hash-2")]},
        "extract_claims": {"claims": [], "latency_ms": 50},
        "resolve_entities": _RESOLVE_RESP,
        "update_sota": {"belief_updates": []},
    }
    fake = _run(db, responses)

    called = {c[0] for c in fake.calls}
    assert "extract_claims" in called
    # Conditional edges must short-circuit: neither entity resolution nor sota ran.
    assert "resolve_entities" not in called
    assert "update_sota" not in called

    conn = get_connection(db)
    assert len(list_sources(conn, limit=50)) == 1  # source still ingested
    assert list_claims(conn, limit=50) == []
    assert list_entities(conn, limit=50) == []
    assert list_beliefs(conn, limit=50) == []
    runs = list_pipeline_runs(conn, limit=5, run_type="ingest")
    assert len(runs) == 1 and runs[0].claims_inserted == 0
    conn.close()


def test_dedup_second_run_inserts_nothing(tmp_path: Path) -> None:
    db = str(tmp_path / "coord.db")
    conn = get_connection(db)
    conn.close()

    _run(db, _full_responses())
    _run(db, _full_responses())  # same hash → deduped

    conn = get_connection(db)
    assert len(list_sources(conn, limit=50)) == 1
    assert len(list_claims(conn, limit=50)) == 1
    runs = list_pipeline_runs(conn, limit=5, run_type="ingest")
    assert len(runs) == 2
    assert runs[0].sources_inserted == 0  # newest run added nothing
    conn.close()


def test_checkpoint_thread_is_run_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The graph checkpoints under thread_id == run_id (in-memory saver here)."""
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from langgraph.checkpoint.memory import InMemorySaver

    monkeypatch.delenv("LANGGRAPH_POSTGRES_URL", raising=False)

    db = str(tmp_path / "coord.db")
    conn = get_connection(db)
    conn.close()

    saver = InMemorySaver()

    @asynccontextmanager
    async def fake_open() -> AsyncIterator[InMemorySaver]:
        yield saver

    monkeypatch.setattr("mesh_pipeline.coordinator.open_checkpointer", fake_open)

    fake = _FakeA2AClient(_full_responses())
    from mesh_pipeline.coordinator import run_pipeline

    with patch("mesh_pipeline.coordinator.MeshA2AClient", return_value=fake):
        asyncio.run(
            run_pipeline(categories=["cs.AI"], max_papers=5, since=None, db_path=db)
        )

    threads = {t.config["configurable"]["thread_id"] for t in saver.list(None)}
    assert len(threads) == 1
    # The single thread id is the run id recorded in pipeline_runs.
    conn = get_connection(db)
    run = list_pipeline_runs(conn, limit=1, run_type="ingest")[0]
    conn.close()
    assert threads == {run.id}
