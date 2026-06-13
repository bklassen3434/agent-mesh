"""Phase 17a — field isolation invariants.

Proves the hard guarantees of the universal field_id partition:
  * the ai-robotics field is seeded with a full profile;
  * entity resolution (blocking + the string fast-path) never crosses fields,
    so the same name in two fields stays distinct;
  * procedural heuristics never leak across fields;
  * episodic recall never leaks across fields;
  * the processed_items dedup ledger is per-field (one source, two fields,
    processed independently).
"""
from __future__ import annotations

from datetime import UTC, datetime

from mesh_agents.entity_resolution import _find_by_name_or_alias
from mesh_db.claims import create_claim
from mesh_db.connection import MeshConnection
from mesh_db.entities import (
    create_entity,
    find_candidate_duplicates,
    set_entity_embedding,
)
from mesh_db.episodic import recall_history
from mesh_db.fields import create_field, get_field, get_field_by_slug
from mesh_db.heuristics import create_heuristic, list_applicable_heuristics
from mesh_db.processed_items import ProcessedDecision, decide, record_processed_item
from mesh_db.sources import create_source
from mesh_models.claim import Claim
from mesh_models.entity import Entity, EntityType
from mesh_models.field import DEFAULT_FIELD_ID, Field, FieldProfile
from mesh_models.heuristic import AgentHeuristic
from mesh_models.source import Source, SourceType

_OTHER = "agribusiness"


def _ensure_other_field(conn: MeshConnection) -> str:
    """Create a second field (idempotent — the fields table survives the
    per-test knowledge truncation, so guard on existence)."""
    existing = get_field_by_slug(conn, _OTHER)
    if existing is not None:
        return existing.id
    create_field(
        conn,
        Field(
            id=_OTHER,
            name="Agribusiness",
            slug=_OTHER,
            profile=FieldProfile(slug=_OTHER, name="Agribusiness", description="farming"),
        ),
    )
    return _OTHER


def _vec(seed: float) -> list[float]:
    """A deterministic 384-dim unit-ish vector; identical seed → identical vec."""
    return [seed] * 384


def test_ai_robotics_field_seeded_with_profile(tmp_db: MeshConnection) -> None:
    field = get_field(tmp_db, DEFAULT_FIELD_ID)
    assert field is not None
    assert field.slug == "ai-robotics"
    # init_pg's Python seed materialized the full canonical profile.
    assert "robotics" in field.profile.description.lower()
    assert field.profile.entity_type_hints  # non-empty


def test_resolution_blocking_never_crosses_fields(tmp_db: MeshConnection) -> None:
    other = _ensure_other_field(tmp_db)
    # Same name, same embedding, two different fields.
    a = Entity(canonical_name="Apple", type=EntityType.lab)
    b = Entity(canonical_name="Apple", type=EntityType.lab)
    create_entity(tmp_db, a, field_id=DEFAULT_FIELD_ID)
    create_entity(tmp_db, b, field_id=other)
    set_entity_embedding(tmp_db, a.id, _vec(0.5))
    set_entity_embedding(tmp_db, b.id, _vec(0.5))

    # Blocking in the agribusiness field must not surface the ai-robotics Apple.
    cands = find_candidate_duplicates(
        tmp_db, _vec(0.5), entity_type=EntityType.lab, field_id=other
    )
    ids = {c[0] for c in cands}
    assert b.id in ids
    assert a.id not in ids


def test_name_fastpath_never_crosses_fields(tmp_db: MeshConnection) -> None:
    other = _ensure_other_field(tmp_db)
    a = Entity(canonical_name="Orange", type=EntityType.concept)
    b = Entity(canonical_name="Orange", type=EntityType.concept)
    create_entity(tmp_db, a, field_id=DEFAULT_FIELD_ID)
    create_entity(tmp_db, b, field_id=other)

    hit_default = _find_by_name_or_alias(tmp_db, "Orange", field_id=DEFAULT_FIELD_ID)
    hit_other = _find_by_name_or_alias(tmp_db, "Orange", field_id=other)
    assert hit_default is not None and hit_default[0] == a.id
    assert hit_other is not None and hit_other[0] == b.id


def test_heuristics_never_leak_across_fields(tmp_db: MeshConnection) -> None:
    other = _ensure_other_field(tmp_db)
    h = AgentHeuristic(
        agent="claim_extractor",
        skill="extract_claims",
        heuristic="prefer benchmark scores with named datasets",
    )
    create_heuristic(tmp_db, h, field_id=DEFAULT_FIELD_ID)

    in_default = list_applicable_heuristics(
        tmp_db, "claim_extractor", "extract_claims", field_id=DEFAULT_FIELD_ID
    )
    in_other = list_applicable_heuristics(
        tmp_db, "claim_extractor", "extract_claims", field_id=other
    )
    assert any(x.id == h.id for x in in_default)
    assert all(x.id != h.id for x in in_other)


def test_episodic_recall_never_leaks_across_fields(tmp_db: MeshConnection) -> None:
    other = _ensure_other_field(tmp_db)
    now = datetime.now(UTC)
    ent = Entity(canonical_name="Tractor", type=EntityType.model)
    create_entity(tmp_db, ent, field_id=DEFAULT_FIELD_ID)
    src = Source(
        type=SourceType.arxiv,
        url="http://example.com/x",
        published_at=now,
        raw_content_hash="hh",
    )
    create_source(tmp_db, src, field_id=DEFAULT_FIELD_ID)
    claim = Claim(
        predicate="has_capability",
        subject_entity_id=ent.id,
        object={"capability": "plowing"},
        source_id=src.id,
        extracted_by_agent="claim_extractor",
        raw_excerpt="it plows",
    )
    create_claim(tmp_db, claim, field_id=DEFAULT_FIELD_ID)

    in_default = recall_history(
        tmp_db, "claim_extractor", field_id=DEFAULT_FIELD_ID
    )
    in_other = recall_history(tmp_db, "claim_extractor", field_id=other)
    assert len(in_default) >= 1
    assert in_other == []


def test_processed_items_ledger_is_per_field(tmp_db: MeshConnection) -> None:
    other = _ensure_other_field(tmp_db)
    record_processed_item(
        tmp_db, "arxiv", "ext-1", "hash-1", field_id=DEFAULT_FIELD_ID
    )
    # Same external id, other field → still unseen (independent ledger).
    assert (
        decide(tmp_db, "arxiv", "ext-1", "hash-1", field_id=other)
        is ProcessedDecision.unseen
    )
    assert (
        decide(tmp_db, "arxiv", "ext-1", "hash-1", field_id=DEFAULT_FIELD_ID)
        is ProcessedDecision.unchanged
    )
