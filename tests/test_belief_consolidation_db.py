"""Phase 19b — belief-merge DB surface tests.

Block → choose → merge, the append-only world-model analog of entity merge.
Uses the ``tmp_db`` writer connection from conftest (pgvector testcontainer);
embeddings are constructed deterministically so CI never downloads a model
(same trick as test_entity_resolution).
"""
from __future__ import annotations

from datetime import UTC, datetime

from mesh_db.beliefs import (
    FAMILY_CAPABILITY,
    FAMILY_SCORE,
    belief_family,
    choose_canonical_belief,
    create_belief,
    find_candidate_duplicate_beliefs,
    get_belief_by_id,
    merge_beliefs,
    set_belief_embedding,
)
from mesh_db.connection import MeshConnection
from mesh_db.fields import create_field
from mesh_db.investigations import create_investigation, get_investigation_by_id
from mesh_db.revisions import list_revisions
from mesh_llm import EMBED_DIM
from mesh_models.belief import Belief
from mesh_models.field import Field, FieldProfile
from mesh_models.investigation import Investigation


def _unit(idx: int, dim: int = EMBED_DIM) -> list[float]:
    """A unit basis vector: distinct indices are orthogonal (cosine distance 1),
    same index is identical (cosine distance 0)."""
    vec = [0.0] * dim
    vec[idx % dim] = 1.0
    return vec


def _make_belief(
    conn: MeshConnection,
    topic: str,
    statement: str,
    idx: int,
    *,
    field_id: str = "ai-robotics",
    supporting: list[str] | None = None,
    held: bool = True,
    revision_count: int = 0,
    last_revised_at: datetime | None = None,
) -> str:
    b = Belief(
        topic=topic,
        statement=statement,
        supporting_claim_ids=supporting or [],
        is_currently_held=held,
        revision_count=revision_count,
        last_revised_at=last_revised_at or datetime.now(UTC),
    )
    create_belief(conn, b, field_id=field_id)
    set_belief_embedding(conn, b.id, _unit(idx))
    return b.id


# --------------------------------------------------------------------------
# family
# --------------------------------------------------------------------------


def test_belief_family_mapping() -> None:
    assert belief_family("sota:imagenet") == FAMILY_SCORE
    assert belief_family("capability:abc-123") == FAMILY_CAPABILITY
    assert belief_family("misc:topic") == "other"


# --------------------------------------------------------------------------
# blocking
# --------------------------------------------------------------------------


def test_blocking_orders_by_similarity_and_excludes_self(tmp_db: MeshConnection) -> None:
    query_id = _make_belief(tmp_db, "sota:a", "Statement A", idx=0)
    near_id = _make_belief(tmp_db, "sota:b", "Statement B", idx=0)  # identical vec
    far_id = _make_belief(tmp_db, "sota:c", "Statement C", idx=5)  # orthogonal
    hits = find_candidate_duplicate_beliefs(tmp_db, _unit(0), exclude_id=query_id)
    ids = [h[0] for h in hits]
    assert query_id not in ids
    assert ids[0] == near_id  # nearest first
    assert hits[0][3] < hits[-1][3]  # distance ascending
    assert far_id in ids


def test_blocking_excludes_not_held(tmp_db: MeshConnection) -> None:
    held = _make_belief(tmp_db, "sota:a", "A", idx=0)
    _make_belief(tmp_db, "sota:b", "B", idx=0, held=False)
    hits = find_candidate_duplicate_beliefs(tmp_db, _unit(0))
    assert {h[0] for h in hits} == {held}


def test_blocking_is_field_scoped(tmp_db: MeshConnection) -> None:
    create_field(
        tmp_db,
        Field(
            id="robotics",
            name="Robotics",
            slug="robotics",
            profile=FieldProfile(slug="robotics", name="Robotics", description="robots"),
        ),
    )
    here = _make_belief(tmp_db, "sota:a", "A", idx=0, field_id="ai-robotics")
    _make_belief(tmp_db, "sota:b", "B", idx=0, field_id="robotics")
    hits = find_candidate_duplicate_beliefs(tmp_db, _unit(0), field_id="ai-robotics")
    assert {h[0] for h in hits} == {here}


def test_blocking_restricts_to_family(tmp_db: MeshConnection) -> None:
    score = _make_belief(tmp_db, "sota:a", "A", idx=0)
    _make_belief(tmp_db, "capability:x", "X", idx=0)  # identical vec, other family
    hits = find_candidate_duplicate_beliefs(tmp_db, _unit(0), family=FAMILY_SCORE)
    assert {h[0] for h in hits} == {score}


# --------------------------------------------------------------------------
# choose_canonical
# --------------------------------------------------------------------------


def test_choose_canonical_prefers_more_supporting_claims(tmp_db: MeshConnection) -> None:
    a = _make_belief(tmp_db, "sota:a", "A", idx=0, supporting=["c1", "c2"])
    b = _make_belief(tmp_db, "sota:b", "B", idx=1, supporting=["c3"])
    canonical, duplicate = choose_canonical_belief(tmp_db, a, b)
    assert canonical == a
    assert duplicate == b


def test_choose_canonical_tiebreaks_by_revision_count(tmp_db: MeshConnection) -> None:
    a = _make_belief(tmp_db, "sota:a", "A", idx=0, supporting=["c1"], revision_count=1)
    b = _make_belief(tmp_db, "sota:b", "B", idx=1, supporting=["c2"], revision_count=4)
    canonical, _ = choose_canonical_belief(tmp_db, a, b)
    assert canonical == b


# --------------------------------------------------------------------------
# merge
# --------------------------------------------------------------------------


def test_merge_folds_claims_and_marks_duplicate_not_held(tmp_db: MeshConnection) -> None:
    canonical = _make_belief(tmp_db, "sota:a", "A", idx=0, supporting=["c1", "c2"])
    duplicate = _make_belief(tmp_db, "sota:b", "B", idx=1, supporting=["c2", "c3"])

    merge_beliefs(tmp_db, canonical, duplicate)

    canon = get_belief_by_id(tmp_db, canonical)
    dup = get_belief_by_id(tmp_db, duplicate)
    assert canon is not None and dup is not None
    # Claim ids unioned (c3 folded in, c2 not double-counted).
    assert canon.supporting_claim_ids == ["c1", "c2", "c3"]
    assert canon.is_currently_held is True
    assert canon.revision_count == 1
    # Duplicate absorbed, not deleted.
    assert dup.is_currently_held is False
    assert dup.revision_count == 1

    # A revision appended to BOTH beliefs.
    canon_revs = list_revisions(tmp_db, belief_id=canonical)
    dup_revs = list_revisions(tmp_db, belief_id=duplicate)
    assert len(canon_revs) == 1
    assert canon_revs[0].revised_by_agent == "belief_consolidator"
    assert canon_revs[0].trigger_claim_ids == ["c3"]
    assert duplicate in canon_revs[0].rationale
    assert len(dup_revs) == 1
    assert canonical in dup_revs[0].rationale


def test_merge_recomputes_confidence_via_fn(tmp_db: MeshConnection) -> None:
    canonical = _make_belief(tmp_db, "sota:a", "A", idx=0, supporting=["c1"])
    duplicate = _make_belief(tmp_db, "sota:b", "B", idx=1, supporting=["c2"])

    merge_beliefs(tmp_db, canonical, duplicate, confidence_fn=lambda _c, _bid: 0.77)

    canon = get_belief_by_id(tmp_db, canonical)
    assert canon is not None
    assert abs(canon.confidence - 0.77) < 1e-9
    rev = list_revisions(tmp_db, belief_id=canonical)[0]
    assert abs(rev.new_confidence - 0.77) < 1e-9


def test_merge_repoints_investigation_belief_refs(tmp_db: MeshConnection) -> None:
    canonical = _make_belief(tmp_db, "sota:a", "A", idx=0)
    duplicate = _make_belief(tmp_db, "sota:b", "B", idx=1)
    inv = Investigation(
        question="q",
        opened_by_belief_id=duplicate,
        resolution_belief_id=duplicate,
    )
    create_investigation(tmp_db, inv)

    merge_beliefs(tmp_db, canonical, duplicate)

    refetched = get_investigation_by_id(tmp_db, inv.id)
    assert refetched is not None
    assert refetched.opened_by_belief_id == canonical
    assert refetched.resolution_belief_id == canonical


def test_merge_is_idempotent_on_absorbed_duplicate(tmp_db: MeshConnection) -> None:
    canonical = _make_belief(tmp_db, "sota:a", "A", idx=0, supporting=["c1"])
    duplicate = _make_belief(tmp_db, "sota:b", "B", idx=1, supporting=["c2"])

    merge_beliefs(tmp_db, canonical, duplicate)
    # Second call: duplicate already not-held → no-op (no new revisions).
    merge_beliefs(tmp_db, canonical, duplicate)

    canon = get_belief_by_id(tmp_db, canonical)
    assert canon is not None
    assert canon.revision_count == 1
    assert len(list_revisions(tmp_db, belief_id=canonical)) == 1
