from __future__ import annotations

import duckdb
import pytest
from mesh_db.beliefs import create_belief, get_belief_by_id, list_beliefs, update_belief
from mesh_db.revisions import create_revision, get_revision_by_id, list_revisions
from mesh_models.belief import Belief
from mesh_models.revision import BeliefRevision


def _make_belief(**kwargs: object) -> Belief:
    defaults: dict[str, object] = {
        "topic": "llm-scaling",
        "statement": "Larger models perform better on benchmarks.",
    }
    defaults.update(kwargs)
    return Belief(**defaults)  # type: ignore[arg-type]


def test_create_and_get_belief(tmp_db: duckdb.DuckDBPyConnection) -> None:
    b = _make_belief()
    create_belief(tmp_db, b)
    fetched = get_belief_by_id(tmp_db, b.id)
    assert fetched is not None
    assert fetched.topic == "llm-scaling"
    assert fetched.is_currently_held is True


def test_supporting_claims_round_trip(tmp_db: duckdb.DuckDBPyConnection) -> None:
    b = _make_belief(supporting_claim_ids=["c1", "c2"])
    create_belief(tmp_db, b)
    fetched = get_belief_by_id(tmp_db, b.id)
    assert fetched is not None
    assert set(fetched.supporting_claim_ids) == {"c1", "c2"}


def test_contradicting_claims_round_trip(tmp_db: duckdb.DuckDBPyConnection) -> None:
    b = _make_belief(contradicting_claim_ids=["d1"])
    create_belief(tmp_db, b)
    fetched = get_belief_by_id(tmp_db, b.id)
    assert fetched is not None
    assert "d1" in fetched.contradicting_claim_ids


def test_list_by_topic(tmp_db: duckdb.DuckDBPyConnection) -> None:
    create_belief(tmp_db, _make_belief(topic="robotics"))
    create_belief(tmp_db, _make_belief(topic="llm-scaling"))
    robotics = list_beliefs(tmp_db, topic="robotics")
    assert all("robotics" in b.topic for b in robotics)


def test_list_currently_held(tmp_db: duckdb.DuckDBPyConnection) -> None:
    create_belief(tmp_db, _make_belief(is_currently_held=True))
    create_belief(tmp_db, _make_belief(is_currently_held=False))
    held = list_beliefs(tmp_db, currently_held=True)
    assert all(b.is_currently_held for b in held)


def test_update_belief_statement(tmp_db: duckdb.DuckDBPyConnection) -> None:
    b = _make_belief()
    create_belief(tmp_db, b)
    updated = update_belief(tmp_db, b.id, statement="Scaling laws hold up to ~1T params.")
    assert "1T" in updated.statement


def test_revision_create_and_get(tmp_db: duckdb.DuckDBPyConnection) -> None:
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


def test_list_revisions_by_belief(tmp_db: duckdb.DuckDBPyConnection) -> None:
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


def test_revision_fk_constraint(tmp_db: duckdb.DuckDBPyConnection) -> None:
    rev = BeliefRevision(
        belief_id="nonexistent-belief",
        previous_statement="old",
        new_statement="new",
        previous_confidence=0.5,
        new_confidence=0.6,
        revised_by_agent="synth",
        rationale="test",
    )
    with pytest.raises(duckdb.Error):
        create_revision(tmp_db, rev)
