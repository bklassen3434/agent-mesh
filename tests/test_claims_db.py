from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest
from mesh_db.claims import (
    backfill_claim_types,
    count_claims,
    create_claim,
    get_claim_by_id,
    list_claims,
    update_claim_status,
)
from mesh_db.connection import MeshConnection
from mesh_db.entities import create_entity
from mesh_db.sources import create_source
from mesh_models.claim import Claim, ClaimStatus, ClaimType
from mesh_models.entity import Entity, EntityType
from mesh_models.source import Source, SourceType


def _setup(conn: MeshConnection) -> tuple[str, str]:
    e = Entity(canonical_name="GPT-4", type=EntityType.model)
    s = Source(
        type=SourceType.arxiv,
        url="https://arxiv.org/abs/test",
        published_at=datetime.now(UTC),
        raw_content_hash="hash1",
    )
    create_entity(conn, e)
    create_source(conn, s)
    return e.id, s.id


def _make_claim(entity_id: str, source_id: str, **kwargs: object) -> Claim:
    defaults: dict[str, object] = {
        "predicate": "has_parameter_count",
        "subject_entity_id": entity_id,
        "object": {"value": "175B"},
        "source_id": source_id,
        "extracted_by_agent": "scout",
        "raw_excerpt": "Model has 175B parameters.",
    }
    defaults.update(kwargs)
    return Claim(**defaults)  # type: ignore[arg-type]


def test_create_and_get(tmp_db: MeshConnection) -> None:
    eid, sid = _setup(tmp_db)
    c = _make_claim(eid, sid)
    create_claim(tmp_db, c)
    fetched = get_claim_by_id(tmp_db, c.id)
    assert fetched is not None
    assert fetched.predicate == "has_parameter_count"


def test_object_json_round_trip(tmp_db: MeshConnection) -> None:
    eid, sid = _setup(tmp_db)
    c = _make_claim(eid, sid, object={"value": "175B", "unit": "params"})
    create_claim(tmp_db, c)
    fetched = get_claim_by_id(tmp_db, c.id)
    assert fetched is not None
    assert fetched.object["unit"] == "params"


def test_list_by_entity(tmp_db: MeshConnection) -> None:
    eid, sid = _setup(tmp_db)
    create_claim(tmp_db, _make_claim(eid, sid, predicate="p1"))
    create_claim(tmp_db, _make_claim(eid, sid, predicate="p2"))
    result = list_claims(tmp_db, entity_id=eid)
    assert len(result) == 2


def test_list_by_status(tmp_db: MeshConnection) -> None:
    eid, sid = _setup(tmp_db)
    c = _make_claim(eid, sid)
    create_claim(tmp_db, c)
    update_claim_status(tmp_db, c.id, ClaimStatus.retracted)
    retracted = list_claims(tmp_db, status=ClaimStatus.retracted)
    assert any(r.id == c.id for r in retracted)


def test_update_claim_status(tmp_db: MeshConnection) -> None:
    eid, sid = _setup(tmp_db)
    c1 = _make_claim(eid, sid, predicate="original")
    c2 = _make_claim(eid, sid, predicate="replacement")
    create_claim(tmp_db, c1)
    create_claim(tmp_db, c2)
    updated = update_claim_status(tmp_db, c1.id, ClaimStatus.superseded, superseded_by=c2.id)
    assert updated.status == ClaimStatus.superseded
    assert updated.superseded_by_claim_id == c2.id


def test_content_fields_not_mutable(tmp_db: MeshConnection) -> None:
    """No general update_claim function exists — only update_claim_status."""
    from mesh_db import claims as claims_module
    assert not hasattr(claims_module, "update_claim")


def test_claim_type_round_trips(tmp_db: MeshConnection) -> None:
    eid, sid = _setup(tmp_db)
    c = _make_claim(eid, sid, predicate="outperforms",
                    object={"compared_to": "GPT-3", "on": "MMLU"})
    assert c.claim_type == ClaimType.comparison  # derived in the model
    create_claim(tmp_db, c)
    fetched = get_claim_by_id(tmp_db, c.id)
    assert fetched is not None
    assert fetched.claim_type == ClaimType.comparison


def test_list_and_count_filter_by_claim_type(tmp_db: MeshConnection) -> None:
    eid, sid = _setup(tmp_db)
    create_claim(tmp_db, _make_claim(eid, sid, predicate="achieves_score",
                                     object={"score": 90, "benchmark": "MMLU"}))
    create_claim(tmp_db, _make_claim(eid, sid, predicate="has_capability",
                                     object={"capability": "long context"}))
    caps = list_claims(tmp_db, claim_type=ClaimType.capability)
    assert len(caps) == 1 and caps[0].claim_type == ClaimType.capability
    assert count_claims(tmp_db, claim_type=ClaimType.score) == 1


def test_check_constraint_rejects_bad_claim_type(tmp_db: MeshConnection) -> None:
    eid, sid = _setup(tmp_db)
    with pytest.raises(psycopg.errors.Error):
        tmp_db.execute(
            "INSERT INTO claims (id, predicate, claim_type, subject_entity_id, "
            "object, source_id, extracted_at, extracted_by_agent, raw_excerpt, "
            "status, confidence) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            ["badtype-1", "p", "not_a_real_type", eid, "{}", sid,
             datetime.now(UTC), "a", "x", "active", 0.5],
        )


def test_backfill_claim_types_repairs_drift(tmp_db: MeshConnection) -> None:
    eid, sid = _setup(tmp_db)
    c = _make_claim(eid, sid, predicate="developed_by", object={"lab": "OpenAI"})
    create_claim(tmp_db, c)
    # Force a drifted claim_type directly, bypassing the model derivation.
    tmp_db.execute(
        "UPDATE claims SET claim_type = 'speculative' WHERE id = %s", [c.id]
    )
    updated = backfill_claim_types(tmp_db)
    assert updated == 1
    fetched = get_claim_by_id(tmp_db, c.id)
    assert fetched is not None and fetched.claim_type == ClaimType.attribution
    # Idempotent: a second pass changes nothing.
    assert backfill_claim_types(tmp_db) == 0


def test_fk_constraint_missing_entity(tmp_db: MeshConnection) -> None:
    s = Source(
        type=SourceType.arxiv,
        url="u",
        published_at=datetime.now(UTC),
        raw_content_hash="h",
    )
    create_source(tmp_db, s)
    c = Claim(
        predicate="p", subject_entity_id="nonexistent-entity",
        object={}, source_id=s.id,
        extracted_by_agent="a", raw_excerpt="",
    )
    with pytest.raises(psycopg.errors.Error):
        create_claim(tmp_db, c)


def test_unsynthesized_excludes_edge_and_nonsynthesizable_claims(
    tmp_db: MeshConnection,
) -> None:
    """unsynthesized_claim_counts_by_entity must skip claims already turned into a
    relationship edge, and claim types that never synthesize (critique /
    reproduction / speculative) — so edge-only / non-synthesizable entities stop
    re-triggering the synthesize-belief tension forever."""
    from mesh_db.claims import unsynthesized_claim_counts_by_entity
    from mesh_db.relationships import create_relationship
    from mesh_models.relationship import Relationship

    eid, sid = _setup(tmp_db)
    e2 = Entity(canonical_name="GPT-3.5", type=EntityType.model)
    create_entity(tmp_db, e2)

    # belief-forming → SHOULD count as unsynthesized
    create_claim(
        tmp_db,
        _make_claim(eid, sid, predicate="achieves_score",
                    object={"score": 90.0, "benchmark": "MMLU"}),
    )
    # non-synthesizable type → excluded by claim_type
    create_claim(tmp_db, _make_claim(eid, sid, predicate="critiques", object={}))
    # edge-forming claim already backing a relationship → excluded via evidence
    cmp_ = _make_claim(eid, sid, predicate="outperforms",
                       object={"compared_to": "GPT-3.5", "on": "MMLU"})
    create_claim(tmp_db, cmp_)
    create_relationship(
        tmp_db,
        Relationship(from_entity_id=eid, to_entity_id=e2.id, type="outperforms",
                     evidence_claim_ids=[cmp_.id]),
    )

    counts = dict(unsynthesized_claim_counts_by_entity(tmp_db))
    assert counts.get(eid) == 1  # only the score claim remains unsynthesized


def test_unsynthesized_excludes_marked_synthesized_claims(
    tmp_db: MeshConnection,
) -> None:
    """A claim recorded in synthesized_claims is excluded from the count even
    though it's in no belief — the terminal state that stops synthesize-belief
    re-firing on processed-but-unmembered claims (non-leader scores etc.)."""
    from mesh_db.claims import (
        mark_claims_synthesized,
        unsynthesized_claim_counts_by_entity,
    )

    eid, sid = _setup(tmp_db)
    c1 = _make_claim(eid, sid, predicate="achieves_score",
                     object={"score": 90.0, "benchmark": "MMLU"})
    c2 = _make_claim(eid, sid, predicate="achieves_score",
                     object={"score": 80.0, "benchmark": "MMLU"})
    create_claim(tmp_db, c1)
    create_claim(tmp_db, c2)
    assert dict(unsynthesized_claim_counts_by_entity(tmp_db)).get(eid) == 2

    # Marking one drops it from the count; idempotent re-marking is a no-op.
    assert mark_claims_synthesized(tmp_db, [c1.id]) == 1
    assert mark_claims_synthesized(tmp_db, [c1.id]) == 1
    assert dict(unsynthesized_claim_counts_by_entity(tmp_db)).get(eid) == 1

    mark_claims_synthesized(tmp_db, [c2.id])
    assert eid not in dict(unsynthesized_claim_counts_by_entity(tmp_db))


def test_unsynthesized_excludes_relational_claims_with_no_target(
    tmp_db: MeshConnection,
) -> None:
    """A relational claim whose object names no target entity can never form an
    edge (synthesize-belief mints a target only when one is named), so it must not
    be counted as unsynthesized — otherwise it re-fires the tension forever."""
    from mesh_db.claims import unsynthesized_claim_counts_by_entity

    eid, sid = _setup(tmp_db)
    # outperforms with an empty compared_to → no edge target → un-synthesizable
    create_claim(
        tmp_db,
        _make_claim(eid, sid, predicate="outperforms", object={"on": "MMLU"}),
    )
    # evaluated_on with a real benchmark target → still counts (mintable)
    create_claim(
        tmp_db,
        _make_claim(eid, sid, predicate="evaluated_on", object={"benchmark": "MMLU"}),
    )

    counts = dict(unsynthesized_claim_counts_by_entity(tmp_db))
    assert counts.get(eid) == 1  # only the targeted evaluation claim counts
