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
    runs = list_pipeline_runs(conn, limit=5, run_type="pipeline")
    assert len(runs) == 1
    assert runs[0].claims_inserted == 1
    assert runs[0].beliefs_created == 1
    assert runs[0].finished_at is not None
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
    runs = list_pipeline_runs(conn, limit=5, run_type="pipeline")
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
    runs = list_pipeline_runs(conn, limit=5, run_type="pipeline")
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
    run = list_pipeline_runs(conn, limit=1, run_type="pipeline")[0]
    conn.close()
    assert threads == {run.id}
