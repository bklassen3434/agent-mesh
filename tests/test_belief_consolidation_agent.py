"""Phase 19c — belief-consolidation resolver tests.

Bands + conservative adjudication + the write-free resolve path. Uses the
``tmp_db`` writer connection (pgvector testcontainer) for blocking; a mock
LLMClient and a stub embedder keep the unit deterministic and offline.
"""
from __future__ import annotations

from typing import Any

from mesh_agents.belief_consolidation import (
    BeliefForMatch,
    BeliefMatchDecision,
    BeliefMergeConfig,
    adjudicate_beliefs,
    band,
    make_confidence_fn,
    resolve_belief_duplicates,
)
from mesh_db.beliefs import create_belief, set_belief_embedding
from mesh_db.connection import MeshConnection
from mesh_llm import EMBED_DIM
from mesh_llm.client import LLMResponseError
from mesh_models.belief import Belief


def _unit(idx: int, dim: int = EMBED_DIM) -> list[float]:
    vec = [0.0] * dim
    vec[idx % dim] = 1.0
    return vec


class _StubEmbedder:
    """Maps a belief's statement to a unit vector by a leading "#<idx>" tag, so
    tests control similarity exactly without a real model."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            idx = int(t.split("#", 1)[1].split(" ", 1)[0]) if "#" in t else 0
            out.append(_unit(idx))
        return out


class _MockLLM:
    def __init__(self, same: bool, *, raise_parse: bool = False) -> None:
        self._same = same
        self._raise = raise_parse
        self.calls = 0

    def complete_with_latency(
        self, name: str, system: str, user: str, response_model: Any = None,
        options: Any = None,
    ) -> tuple[Any, int]:
        self.calls += 1
        if self._raise:
            raise LLMResponseError("boom")
        return BeliefMatchDecision(same_proposition=self._same, reason="x"), 5


# --------------------------------------------------------------------------
# bands
# --------------------------------------------------------------------------


def test_band_thresholds() -> None:
    cfg = BeliefMergeConfig(high=0.95, low=0.85)
    assert band(0.96, cfg) == "merge"
    assert band(0.90, cfg) == "adjudicate"
    assert band(0.80, cfg) == "reject"


def test_config_from_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("MESH_BELIEF_MERGE_HIGH", "0.99")
    monkeypatch.setenv("MESH_BELIEF_MERGE_LOW", "0.70")
    cfg = BeliefMergeConfig.from_env()
    assert cfg.high == 0.99
    assert cfg.low == 0.70


# --------------------------------------------------------------------------
# adjudication — conservative
# --------------------------------------------------------------------------


def test_adjudicate_returns_true_when_same() -> None:
    llm = _MockLLM(same=True)
    a = BeliefForMatch("sota:x", "A")
    b = BeliefForMatch("sota:x", "A'")
    assert adjudicate_beliefs(llm, a, b) is True  # type: ignore[arg-type]


def test_adjudicate_defaults_not_same_on_parse_failure() -> None:
    llm = _MockLLM(same=True, raise_parse=True)
    a = BeliefForMatch("sota:x", "A")
    b = BeliefForMatch("sota:x", "A'")
    assert adjudicate_beliefs(llm, a, b) is False  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# resolve_belief_duplicates
# --------------------------------------------------------------------------


def _belief(conn: MeshConnection, topic: str, idx: int) -> Belief:
    b = Belief(topic=topic, statement=f"#{idx} statement")
    create_belief(conn, b)
    set_belief_embedding(conn, b.id, _unit(idx))
    return b


def test_resolve_high_band_confirms_merge(tmp_db: MeshConnection) -> None:
    query = _belief(tmp_db, "sota:a", 0)
    cand = _belief(tmp_db, "sota:b", 0)  # identical vec → similarity 1.0
    decisions = resolve_belief_duplicates(
        tmp_db, query, embedder=_StubEmbedder(), llm=None
    )
    assert len(decisions) == 1
    assert decisions[0].candidate_id == cand.id
    assert decisions[0].band == "merge"
    assert decisions[0].confirmed is True


def test_resolve_low_band_excluded(tmp_db: MeshConnection) -> None:
    query = _belief(tmp_db, "sota:a", 0)
    _belief(tmp_db, "sota:b", 5)  # orthogonal → similarity 0.0 → reject
    decisions = resolve_belief_duplicates(
        tmp_db, query, embedder=_StubEmbedder(), llm=None
    )
    assert decisions == []


def test_resolve_middle_band_adjudicates(tmp_db: MeshConnection) -> None:
    # Build a candidate at ~0.9 similarity (between low 0.85 and high 0.95).
    query = Belief(topic="sota:a", statement="#0 statement")
    create_belief(tmp_db, query)
    set_belief_embedding(tmp_db, query.id, _unit(0))
    cand = Belief(topic="sota:b", statement="#0 cand")
    create_belief(tmp_db, cand)
    # Mix basis vectors 0 and 1 so cosine ≈ 0.9 with _unit(0) query.
    import math

    mixed = [0.0] * EMBED_DIM
    mixed[0] = math.cos(math.radians(25))  # ~0.906
    mixed[1] = math.sin(math.radians(25))
    set_belief_embedding(tmp_db, cand.id, mixed)

    llm_yes = _MockLLM(same=True)
    decisions = resolve_belief_duplicates(
        tmp_db, query, embedder=_StubEmbedder(), llm=llm_yes  # type: ignore[arg-type]
    )
    assert len(decisions) == 1
    assert decisions[0].candidate_id == cand.id
    assert decisions[0].band == "adjudicate"
    assert decisions[0].confirmed is True
    assert llm_yes.calls == 1

    # Same geometry, LLM says not-same → not confirmed.
    llm_no = _MockLLM(same=False)
    decisions_no = resolve_belief_duplicates(
        tmp_db, query, embedder=_StubEmbedder(), llm=llm_no  # type: ignore[arg-type]
    )
    assert decisions_no[0].confirmed is False


def test_resolve_middle_band_defaults_not_same_without_llm(tmp_db: MeshConnection) -> None:
    import math

    query = Belief(topic="sota:a", statement="#0 statement")
    create_belief(tmp_db, query)
    set_belief_embedding(tmp_db, query.id, _unit(0))
    cand = Belief(topic="sota:b", statement="#0 cand")
    create_belief(tmp_db, cand)
    mixed = [0.0] * EMBED_DIM
    mixed[0] = math.cos(math.radians(25))
    mixed[1] = math.sin(math.radians(25))
    set_belief_embedding(tmp_db, cand.id, mixed)

    decisions = resolve_belief_duplicates(
        tmp_db, query, embedder=_StubEmbedder(), llm=None
    )
    assert decisions[0].band == "adjudicate"
    assert decisions[0].confirmed is False


def test_make_confidence_fn_reads_signals(tmp_db: MeshConnection) -> None:
    b = _belief(tmp_db, "sota:a", 0)
    fn = make_confidence_fn()
    # No claims linked → all-zero signals → base confidence (0.5 default).
    assert abs(fn(tmp_db, b.id) - 0.5) < 1e-9
