from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from mesh_db.connection import MeshConnection
from mesh_db.entities import create_entity
from mesh_db.heuristics import (
    create_heuristic,
    create_heuristic_revision,
    get_heuristic_by_id,
    list_applicable_heuristics,
    list_heuristic_revisions,
    list_heuristics,
    update_heuristic,
)
from mesh_models.entity import Entity, EntityType
from mesh_models.heuristic import AgentHeuristic, AgentHeuristicRevision


def _make_heuristic(**kwargs: object) -> AgentHeuristic:
    defaults: dict[str, object] = {
        "agent": "claim_extractor",
        "skill": "extract_claims",
        "heuristic": "Forum scores are self-reported; lower their confidence.",
        "provenance_run_ids": ["run-1"],
        "provenance_claim_ids": ["claim-1"],
    }
    defaults.update(kwargs)
    return AgentHeuristic(**defaults)  # type: ignore[arg-type]


def test_create_and_get_heuristic(tmp_db: MeshConnection) -> None:
    h = _make_heuristic()
    create_heuristic(tmp_db, h)
    fetched = get_heuristic_by_id(tmp_db, h.id)
    assert fetched is not None
    assert fetched.agent == "claim_extractor"
    assert fetched.confidence == pytest.approx(0.3)
    assert fetched.is_currently_active is True
    assert fetched.provenance_run_ids == ["run-1"]
    assert fetched.provenance_claim_ids == ["claim-1"]


def test_entity_scope_fk(tmp_db: MeshConnection) -> None:
    ent = create_entity(tmp_db, Entity(canonical_name="Mamba", type=EntityType.model))
    h = _make_heuristic(entity_id=ent.id)
    create_heuristic(tmp_db, h)
    fetched = get_heuristic_by_id(tmp_db, h.id)
    assert fetched is not None and fetched.entity_id == ent.id
    # A dangling entity_id is rejected by the FK.
    with pytest.raises(psycopg.errors.Error):
        create_heuristic(tmp_db, _make_heuristic(entity_id="no-such-entity"))


def test_update_and_revision_append_only(tmp_db: MeshConnection) -> None:
    h = _make_heuristic()
    create_heuristic(tmp_db, h)
    update_heuristic(
        tmp_db, h.id, confidence=0.5, revision_count=1,
        last_revised_at=datetime.now(UTC),
    )
    create_heuristic_revision(
        tmp_db,
        AgentHeuristicRevision(
            heuristic_id=h.id,
            previous_heuristic=h.heuristic,
            new_heuristic=h.heuristic,
            previous_confidence=0.3,
            new_confidence=0.5,
            provenance_run_ids=["run-2"],
            revised_by_agent="consolidator",
            rationale="survived another window",
        ),
    )
    fetched = get_heuristic_by_id(tmp_db, h.id)
    assert fetched is not None and fetched.confidence == pytest.approx(0.5)
    revs = list_heuristic_revisions(tmp_db, heuristic_id=h.id)
    assert len(revs) == 1 and revs[0].new_confidence == pytest.approx(0.5)


def test_revision_fk_constraint(tmp_db: MeshConnection) -> None:
    with pytest.raises(psycopg.errors.Error):
        create_heuristic_revision(
            tmp_db,
            AgentHeuristicRevision(
                heuristic_id="nonexistent",
                previous_heuristic="a",
                new_heuristic="b",
                previous_confidence=0.3,
                new_confidence=0.4,
                revised_by_agent="consolidator",
                rationale="x",
            ),
        )


def test_applicable_excludes_expired_and_inactive(tmp_db: MeshConnection) -> None:
    now = datetime.now(UTC)
    create_heuristic(tmp_db, _make_heuristic(heuristic="live"))
    create_heuristic(
        tmp_db, _make_heuristic(heuristic="expired", expires_at=now - timedelta(days=1))
    )
    create_heuristic(
        tmp_db, _make_heuristic(heuristic="retired", is_currently_active=False)
    )
    out = list_applicable_heuristics(tmp_db, "claim_extractor", "extract_claims", now=now)
    texts = {h.heuristic for h in out}
    assert texts == {"live"}


def test_applicable_scope_matching(tmp_db: MeshConnection) -> None:
    now = datetime.now(UTC)
    create_heuristic(tmp_db, _make_heuristic(heuristic="broad"))  # source NULL
    create_heuristic(tmp_db, _make_heuristic(heuristic="forum-only", source="reddit"))
    # No source given → only the broadly-scoped heuristic applies.
    no_src = {h.heuristic for h in list_applicable_heuristics(
        tmp_db, "claim_extractor", "extract_claims", now=now)}
    assert no_src == {"broad"}
    # Matching source → both the broad and the source-specific heuristic apply.
    with_src = {h.heuristic for h in list_applicable_heuristics(
        tmp_db, "claim_extractor", "extract_claims", source="reddit", now=now)}
    assert with_src == {"broad", "forum-only"}
    # Wrong skill → nothing matches.
    other_skill = list_applicable_heuristics(
        tmp_db, "claim_extractor", "challenge_belief", now=now)
    assert other_skill == []


def test_list_heuristics_filters(tmp_db: MeshConnection) -> None:
    now = datetime.now(UTC)
    create_heuristic(tmp_db, _make_heuristic(agent="skeptic", skill="challenge_belief"))
    create_heuristic(
        tmp_db, _make_heuristic(heuristic="expired", expires_at=now - timedelta(days=1))
    )
    skeptic_only = list_heuristics(tmp_db, agent="skeptic")
    assert all(h.agent == "skeptic" for h in skeptic_only) and len(skeptic_only) == 1
    unexpired = list_heuristics(tmp_db, include_expired=False, now=now)
    assert all(h.heuristic != "expired" for h in unexpired)
