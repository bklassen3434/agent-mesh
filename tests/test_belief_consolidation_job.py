"""Phase 19d — belief-consolidation LangGraph job end-to-end.

Runs run_belief_consolidation against the testcontainer DB with the embedder
patched to a deterministic stub and no LLM (high-band auto-merge only). Verifies
the job merges a duplicate, ages the corpus, and writes a pipeline_run row.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

from mesh_db.beliefs import create_belief, get_belief_by_id, list_beliefs
from mesh_db.connection import get_connection
from mesh_db.pipeline_runs import list_pipeline_runs
from mesh_llm import EMBED_DIM
from mesh_llm.client import LLMProviderNotReadyError
from mesh_models.belief import Belief


def _unit(idx: int, dim: int = EMBED_DIM) -> list[float]:
    vec = [0.0] * dim
    vec[idx % dim] = 1.0
    return vec


class _StubEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            idx = int(t.split("#", 1)[1].split(" ", 1)[0]) if "#" in t else 0
            out.append(_unit(idx))
        return out


def _seed_duplicate_pair() -> None:
    conn = get_connection()
    create_belief(
        conn,
        Belief(topic="sota:a", statement="#0 alpha", supporting_claim_ids=["c1", "c2"]),
    )
    create_belief(
        conn,
        Belief(topic="sota:b", statement="#0 beta", supporting_claim_ids=["c3"]),
    )
    conn.close()


def _run() -> Any:
    from mesh_pipeline.belief_consolidation import run_belief_consolidation

    def _no_llm(*_a: Any, **_k: Any) -> Any:
        raise LLMProviderNotReadyError("no provider in test")

    with (
        patch(
            "mesh_pipeline.belief_consolidation._make_embedder",
            return_value=_StubEmbedder(),
        ),
        patch("mesh_pipeline.belief_consolidation.make_llm_client", _no_llm),
    ):
        return asyncio.run(run_belief_consolidation())


def test_job_merges_duplicate_and_records_run(tmp_db: Any) -> None:
    _seed_duplicate_pair()
    result = _run()

    assert result.fields_processed >= 1
    assert result.beliefs_merged == 1
    held = list_beliefs(tmp_db, currently_held=True)
    assert len(held) == 1

    runs = list_pipeline_runs(tmp_db, limit=10, run_type="belief_consolidation")
    assert len(runs) == 1


def test_job_noop_on_clean_corpus(tmp_db: Any) -> None:
    # Two unrelated beliefs (orthogonal vectors) → nothing to merge.
    conn = get_connection()
    create_belief(conn, Belief(topic="sota:a", statement="#0 alpha"))
    create_belief(conn, Belief(topic="sota:b", statement="#5 beta"))
    conn.close()

    result = _run()
    assert result.beliefs_merged == 0
    assert len(list_beliefs(tmp_db, currently_held=True)) == 2


def test_job_archives_long_dead_belief(tmp_db: Any) -> None:
    conn = get_connection()
    dead = datetime.now(UTC) - timedelta(days=400)
    b = Belief(
        topic="sota:a", statement="#0 alpha", supporting_claim_ids=[],
        last_revised_at=dead,
    )
    create_belief(conn, b)
    conn.close()

    result = _run()
    assert result.beliefs_archived == 1
    refetched = get_belief_by_id(tmp_db, b.id)
    assert refetched is not None
    assert refetched.is_currently_held is False
