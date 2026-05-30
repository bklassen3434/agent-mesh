from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from mesh_db.beliefs import (
    create_belief,
    find_stale_beliefs,
    get_belief_by_id,
    list_beliefs,
    update_belief,
)
from mesh_db.claims import create_claim
from mesh_db.connection import MeshConnection
from mesh_db.entities import create_entity
from mesh_db.revisions import create_revision, get_revision_by_id, list_revisions
from mesh_db.sources import create_source
from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.entity import Entity, EntityType
from mesh_models.revision import BeliefRevision
from mesh_models.source import Source, SourceType


def _make_belief(**kwargs: object) -> Belief:
    defaults: dict[str, object] = {
        "topic": "llm-scaling",
        "statement": "Larger models perform better on benchmarks.",
    }
    defaults.update(kwargs)
    return Belief(**defaults)  # type: ignore[arg-type]


def test_create_and_get_belief(tmp_db: MeshConnection) -> None:
    b = _make_belief()
    create_belief(tmp_db, b)
    fetched = get_belief_by_id(tmp_db, b.id)
    assert fetched is not None
    assert fetched.topic == "llm-scaling"
    assert fetched.is_currently_held is True


def test_supporting_claims_round_trip(tmp_db: MeshConnection) -> None:
    b = _make_belief(supporting_claim_ids=["c1", "c2"])
    create_belief(tmp_db, b)
    fetched = get_belief_by_id(tmp_db, b.id)
    assert fetched is not None
    assert set(fetched.supporting_claim_ids) == {"c1", "c2"}


def test_contradicting_claims_round_trip(tmp_db: MeshConnection) -> None:
    b = _make_belief(contradicting_claim_ids=["d1"])
    create_belief(tmp_db, b)
    fetched = get_belief_by_id(tmp_db, b.id)
    assert fetched is not None
    assert "d1" in fetched.contradicting_claim_ids


def test_list_by_topic(tmp_db: MeshConnection) -> None:
    create_belief(tmp_db, _make_belief(topic="robotics"))
    create_belief(tmp_db, _make_belief(topic="llm-scaling"))
    robotics = list_beliefs(tmp_db, topic="robotics")
    assert all("robotics" in b.topic for b in robotics)


def test_list_currently_held(tmp_db: MeshConnection) -> None:
    create_belief(tmp_db, _make_belief(is_currently_held=True))
    create_belief(tmp_db, _make_belief(is_currently_held=False))
    held = list_beliefs(tmp_db, currently_held=True)
    assert all(b.is_currently_held for b in held)


def test_update_belief_statement(tmp_db: MeshConnection) -> None:
    b = _make_belief()
    create_belief(tmp_db, b)
    updated = update_belief(tmp_db, b.id, statement="Scaling laws hold up to ~1T params.")
    assert "1T" in updated.statement


def test_revision_create_and_get(tmp_db: MeshConnection) -> None:
    b = _make_belief()
    create_belief(tmp_db, b)
    rev = BeliefRevision(
        belief_id=b.id,
        previous_statement=b.statement,
        new_statement="Updated statement",
        previous_confidence=0.5,
        new_confidence=0.8,
        revised_by_agent="synth",
        rationale="new paper",
    )
    create_revision(tmp_db, rev)
    fetched = get_revision_by_id(tmp_db, rev.id)
    assert fetched is not None
    assert fetched.rationale == "new paper"


def test_list_revisions_by_belief(tmp_db: MeshConnection) -> None:
    b = _make_belief()
    create_belief(tmp_db, b)
    for i in range(3):
        create_revision(
            tmp_db,
            BeliefRevision(
                belief_id=b.id,
                previous_statement="old",
                new_statement=f"new-{i}",
                previous_confidence=0.5,
                new_confidence=0.6,
                revised_by_agent="synth",
                rationale=f"reason-{i}",
            ),
        )
    revs = list_revisions(tmp_db, belief_id=b.id)
    assert len(revs) == 3


def _seed_claim(
    conn: MeshConnection, extracted_at: datetime
) -> str:
    """Insert a claim with the given timestamp and return its id."""
    now = datetime.now(UTC)
    entity = create_entity(conn, Entity(canonical_name="Model X", type=EntityType.model))
    source = create_source(
        conn,
        Source(
            type=SourceType.arxiv,
            url=f"http://test/{extracted_at.isoformat()}",
            published_at=now,
            raw_content_hash=f"h-{extracted_at.isoformat()}",
            fetched_at=now,
        ),
    )
    claim = create_claim(
        conn,
        Claim(
            predicate="evaluated_on",
            subject_entity_id=entity.id,
            object={"benchmark": "MMLU"},
            source_id=source.id,
            extracted_at=extracted_at,
            extracted_by_agent="t",
            raw_excerpt="x",
            confidence=0.9,
        ),
    )
    return claim.id


def test_find_stale_beliefs_orders_no_claims_then_oldest(
    tmp_db: MeshConnection,
) -> None:
    now = datetime.now(UTC)
    old_claim_id = _seed_claim(tmp_db, now - timedelta(days=60))
    fresh_claim_id = _seed_claim(tmp_db, now - timedelta(days=1))

    create_belief(
        tmp_db,
        _make_belief(topic="stale", supporting_claim_ids=[old_claim_id]),
    )
    create_belief(
        tmp_db,
        _make_belief(topic="fresh", supporting_claim_ids=[fresh_claim_id]),
    )
    create_belief(tmp_db, _make_belief(topic="no-claims"))

    stale = find_stale_beliefs(tmp_db, threshold_days=30)
    topics = [b.topic for b in stale]
    assert "fresh" not in topics
    assert {"stale", "no-claims"}.issubset(topics)
    # No-claims belief sorts NULLS FIRST so it leads the staler-than list.
    assert stale[0].topic == "no-claims"


def test_find_stale_beliefs_uses_max_across_supporting_and_contradicting(
    tmp_db: MeshConnection,
) -> None:
    now = datetime.now(UTC)
    old_claim_id = _seed_claim(tmp_db, now - timedelta(days=60))
    fresh_claim_id = _seed_claim(tmp_db, now - timedelta(days=1))
    # A belief with an old supporting claim but a fresh contradicting claim
    # should NOT count as stale — fresh evidence still arrived.
    create_belief(
        tmp_db,
        _make_belief(
            topic="contradicted-recently",
            supporting_claim_ids=[old_claim_id],
            contradicting_claim_ids=[fresh_claim_id],
        ),
    )
    stale = find_stale_beliefs(tmp_db, threshold_days=30)
    assert all(b.topic != "contradicted-recently" for b in stale)


def test_find_stale_beliefs_skips_superseded(
    tmp_db: MeshConnection,
) -> None:
    now = datetime.now(UTC)
    old_claim_id = _seed_claim(tmp_db, now - timedelta(days=60))
    create_belief(
        tmp_db,
        _make_belief(
            topic="dropped",
            supporting_claim_ids=[old_claim_id],
            is_currently_held=False,
        ),
    )
    stale = find_stale_beliefs(tmp_db, threshold_days=30)
    assert all(b.topic != "dropped" for b in stale)


def test_revision_fk_constraint(tmp_db: MeshConnection) -> None:
    rev = BeliefRevision(
        belief_id="nonexistent-belief",
        previous_statement="old",
        new_statement="new",
        previous_confidence=0.5,
        new_confidence=0.6,
        revised_by_agent="synth",
        rationale="test",
    )
    with pytest.raises(psycopg.errors.Error):
        create_revision(tmp_db, rev)
