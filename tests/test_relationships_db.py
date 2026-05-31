"""DB tests for claim-grounded edge aggregation (Phase 14c)."""
from __future__ import annotations

from mesh_db.connection import MeshConnection
from mesh_db.entities import create_entity
from mesh_db.relationships import (
    add_relationship_evidence,
    find_relationship,
    list_relationships,
)
from mesh_models.entity import Entity, EntityType


def _two_entities(conn: MeshConnection) -> tuple[str, str]:
    a = Entity(canonical_name="TestModel-7B", type=EntityType.model)
    b = Entity(canonical_name="GPT-3", type=EntityType.model)
    create_entity(conn, a)
    create_entity(conn, b)
    return a.id, b.id


def test_add_evidence_creates_edge(tmp_db: MeshConnection) -> None:
    a, b = _two_entities(tmp_db)
    rel, created = add_relationship_evidence(tmp_db, a, b, "outperforms", "c1", 0.8)
    assert created
    assert rel.type == "outperforms"
    assert rel.evidence_claim_ids == ["c1"]
    found = find_relationship(tmp_db, a, b, "outperforms")
    assert found is not None and found.id == rel.id


def test_repeat_assertion_aggregates_onto_one_edge(tmp_db: MeshConnection) -> None:
    a, b = _two_entities(tmp_db)
    add_relationship_evidence(tmp_db, a, b, "outperforms", "c1", 0.8)
    _rel, created = add_relationship_evidence(tmp_db, a, b, "outperforms", "c2", 0.9)
    assert not created  # aggregated, not duplicated
    edges = list_relationships(tmp_db, from_entity_id=a)
    assert len(edges) == 1
    assert set(edges[0].evidence_claim_ids) == {"c1", "c2"}
    assert edges[0].confidence == 0.9  # lifted to the strongest supporting claim


def test_same_claim_twice_is_idempotent(tmp_db: MeshConnection) -> None:
    a, b = _two_entities(tmp_db)
    add_relationship_evidence(tmp_db, a, b, "developed_by", "c1", 0.7)
    add_relationship_evidence(tmp_db, a, b, "developed_by", "c1", 0.7)
    edges = list_relationships(tmp_db, from_entity_id=a)
    assert len(edges) == 1
    assert edges[0].evidence_claim_ids == ["c1"]


def test_distinct_types_are_distinct_edges(tmp_db: MeshConnection) -> None:
    a, b = _two_entities(tmp_db)
    add_relationship_evidence(tmp_db, a, b, "outperforms", "c1", 0.8)
    add_relationship_evidence(tmp_db, a, b, "based_on", "c2", 0.8)
    edges = list_relationships(tmp_db, from_entity_id=a)
    assert {e.type for e in edges} == {"outperforms", "based_on"}
