"""Phase 19d/19e — belief-consolidation sweep engine tests.

Covers reconcile_beliefs (backfill → block+band → cluster+merge) on the high-band
auto-merge path, plus the LLM-free decay + archival pass. Deterministic stub
embedder; no LLM; pgvector testcontainer via ``tmp_db``.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mesh_agents.belief_reconcile import (
    decay_and_archive,
    reconcile_beliefs,
)
from mesh_db.beliefs import create_belief, get_belief_by_id, list_beliefs
from mesh_db.claims import create_claim
from mesh_db.connection import MeshConnection
from mesh_db.entities import create_entity
from mesh_db.revisions import list_revisions
from mesh_db.sources import create_source
from mesh_llm import EMBED_DIM
from mesh_models.belief import Belief
from mesh_models.claim import Claim, ClaimStatus
from mesh_models.entity import Entity, EntityType
from mesh_models.source import Source, SourceType


def _unit(idx: int, dim: int = EMBED_DIM) -> list[float]:
    vec = [0.0] * dim
    vec[idx % dim] = 1.0
    return vec


class _StubEmbedder:
    """Maps text containing "#<idx>" to a unit basis vector — same idx → identical
    vector (cosine 1.0), distinct idx → orthogonal (cosine 0.0)."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            idx = int(t.split("#", 1)[1].split(" ", 1)[0]) if "#" in t else 0
            out.append(_unit(idx))
        return out


def _belief(
    conn: MeshConnection,
    topic: str,
    tag: int,
    *,
    supporting: list[str] | None = None,
    confidence: float = 0.8,
    last_revised_at: datetime | None = None,
    held: bool = True,
) -> str:
    b = Belief(
        topic=topic,
        statement=f"#{tag} statement for {topic}",
        supporting_claim_ids=supporting or [],
        confidence=confidence,
        last_revised_at=last_revised_at or datetime.now(UTC),
        is_currently_held=held,
    )
    create_belief(conn, b)
    return b.id


# --------------------------------------------------------------------------
# reconcile — high-band auto-merge
# --------------------------------------------------------------------------


def test_reconcile_merges_high_band_duplicate(tmp_db: MeshConnection) -> None:
    a = _belief(tmp_db, "sota:a", 0, supporting=["c1", "c2"])
    b = _belief(tmp_db, "sota:b", 0, supporting=["c3"])  # identical vec → merge

    report = reconcile_beliefs(
        tmp_db, _StubEmbedder(), llm=None, decay=False
    )

    assert report.merges == 1
    assert report.embedded_now == 2
    held = list_beliefs(tmp_db, currently_held=True)
    assert len(held) == 1
    # The more-claimed belief (a) is canonical and absorbs c3.
    canon = get_belief_by_id(tmp_db, a)
    dup = get_belief_by_id(tmp_db, b)
    assert canon is not None and dup is not None
    assert canon.is_currently_held is True
    assert set(canon.supporting_claim_ids) == {"c1", "c2", "c3"}
    assert dup.is_currently_held is False


def test_reconcile_dry_run_writes_nothing(tmp_db: MeshConnection) -> None:
    _belief(tmp_db, "sota:a", 0, supporting=["c1", "c2"])
    b = _belief(tmp_db, "sota:b", 0, supporting=["c3"])

    report = reconcile_beliefs(
        tmp_db, _StubEmbedder(), llm=None, dry_run=True, decay=False
    )
    assert report.merges == 1  # planned
    # ...but nothing applied.
    assert get_belief_by_id(tmp_db, b).is_currently_held is True  # type: ignore[union-attr]
    assert len(list_beliefs(tmp_db, currently_held=True)) == 2


def test_reconcile_does_not_merge_across_families(tmp_db: MeshConnection) -> None:
    _belief(tmp_db, "sota:a", 0)
    cap = _belief(tmp_db, "capability:x", 0)  # identical vec, different family
    report = reconcile_beliefs(tmp_db, _StubEmbedder(), llm=None, decay=False)
    assert report.merges == 0
    assert get_belief_by_id(tmp_db, cap).is_currently_held is True  # type: ignore[union-attr]


# --------------------------------------------------------------------------
# decay + archival (Phase 19e) — LLM-free
# --------------------------------------------------------------------------


def _seed_live_claim(conn: MeshConnection) -> str:
    ent = create_entity(conn, Entity(canonical_name="M", type=EntityType.model))
    src = create_source(
        conn,
        Source(
            type=SourceType.arxiv, url="https://arxiv.org/abs/1",
            published_at=datetime.now(UTC), raw_content_hash="h1",
        ),
    )
    claim = Claim(
        predicate="achieves_score", subject_entity_id=ent.id,
        object={"score": 9.0}, source_id=src.id, extracted_by_agent="x",
        raw_excerpt="e", confidence=0.7, status=ClaimStatus.active,
    )
    create_claim(conn, claim)
    return claim.id


def test_decay_lowers_confidence_with_revision(tmp_db: MeshConnection) -> None:
    cid = _seed_live_claim(tmp_db)
    old = datetime.now(UTC) - timedelta(days=200)  # > 90d half-life, < 365d archive
    bid = _belief(
        tmp_db, "sota:a", 0, supporting=[cid], confidence=0.8, last_revised_at=old
    )
    decayed, archived = decay_and_archive(tmp_db)
    assert decayed == 1
    assert archived == 0
    b = get_belief_by_id(tmp_db, bid)
    assert b is not None
    assert b.confidence < 0.8
    assert b.is_currently_held is True
    revs = list_revisions(tmp_db, belief_id=bid)
    assert revs[0].rationale == "staleness decay"
    assert revs[0].revised_by_agent == "belief_consolidator"


def test_decay_respects_floor(tmp_db: MeshConnection) -> None:
    cid = _seed_live_claim(tmp_db)
    ancient = datetime.now(UTC) - timedelta(days=300)
    bid = _belief(
        tmp_db, "sota:a", 0, supporting=[cid], confidence=0.12, last_revised_at=ancient
    )
    decay_and_archive(tmp_db)
    b = get_belief_by_id(tmp_db, bid)
    assert b is not None
    assert b.confidence >= 0.1  # floor


def test_archive_long_dead_unsupported_belief(tmp_db: MeshConnection) -> None:
    dead = datetime.now(UTC) - timedelta(days=400)  # > 365d, no supporting claims
    bid = _belief(tmp_db, "sota:a", 0, supporting=[], confidence=0.6, last_revised_at=dead)
    _decayed, archived = decay_and_archive(tmp_db)
    assert archived == 1
    b = get_belief_by_id(tmp_db, bid)
    assert b is not None
    assert b.is_currently_held is False
    revs = list_revisions(tmp_db, belief_id=bid)
    assert revs[0].rationale == "archived: stale, no live evidence"


def test_archive_skipped_when_live_evidence_present(tmp_db: MeshConnection) -> None:
    cid = _seed_live_claim(tmp_db)
    dead = datetime.now(UTC) - timedelta(days=400)
    bid = _belief(
        tmp_db, "sota:a", 0, supporting=[cid], confidence=0.6, last_revised_at=dead
    )
    _decayed, archived = decay_and_archive(tmp_db)
    assert archived == 0  # has live evidence → decays instead of archiving
    assert get_belief_by_id(tmp_db, bid).is_currently_held is True  # type: ignore[union-attr]


def test_decay_is_field_scoped(tmp_db: MeshConnection) -> None:
    old = datetime.now(UTC) - timedelta(days=200)
    cid = _seed_live_claim(tmp_db)
    _belief(tmp_db, "sota:a", 0, supporting=[cid], last_revised_at=old)
    # Scope to a different (nonexistent in this run) field → nothing to age.
    decayed, archived = decay_and_archive(tmp_db, field_id="some-other-field")
    assert decayed == 0
    assert archived == 0
