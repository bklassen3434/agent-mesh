from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pytest
from mesh_db.claims import create_claim, get_claim_by_id, list_claims, update_claim_status
from mesh_db.entities import create_entity
from mesh_db.sources import create_source
from mesh_models.claim import Claim, ClaimStatus
from mesh_models.entity import Entity, EntityType
from mesh_models.source import Source, SourceType


def _setup(conn: duckdb.DuckDBPyConnection) -> tuple[str, str]:
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


def test_create_and_get(tmp_db: duckdb.DuckDBPyConnection) -> None:
    eid, sid = _setup(tmp_db)
    c = _make_claim(eid, sid)
    create_claim(tmp_db, c)
    fetched = get_claim_by_id(tmp_db, c.id)
    assert fetched is not None
    assert fetched.predicate == "has_parameter_count"


def test_object_json_round_trip(tmp_db: duckdb.DuckDBPyConnection) -> None:
    eid, sid = _setup(tmp_db)
    c = _make_claim(eid, sid, object={"value": "175B", "unit": "params"})
    create_claim(tmp_db, c)
    fetched = get_claim_by_id(tmp_db, c.id)
    assert fetched is not None
    assert fetched.object["unit"] == "params"


def test_list_by_entity(tmp_db: duckdb.DuckDBPyConnection) -> None:
    eid, sid = _setup(tmp_db)
    create_claim(tmp_db, _make_claim(eid, sid, predicate="p1"))
    create_claim(tmp_db, _make_claim(eid, sid, predicate="p2"))
    result = list_claims(tmp_db, entity_id=eid)
    assert len(result) == 2


def test_list_by_status(tmp_db: duckdb.DuckDBPyConnection) -> None:
    eid, sid = _setup(tmp_db)
    c = _make_claim(eid, sid)
    create_claim(tmp_db, c)
    update_claim_status(tmp_db, c.id, ClaimStatus.retracted)
    retracted = list_claims(tmp_db, status=ClaimStatus.retracted)
    assert any(r.id == c.id for r in retracted)


def test_update_claim_status(tmp_db: duckdb.DuckDBPyConnection) -> None:
    eid, sid = _setup(tmp_db)
    c1 = _make_claim(eid, sid, predicate="original")
    c2 = _make_claim(eid, sid, predicate="replacement")
    create_claim(tmp_db, c1)
    create_claim(tmp_db, c2)
    updated = update_claim_status(tmp_db, c1.id, ClaimStatus.superseded, superseded_by=c2.id)
    assert updated.status == ClaimStatus.superseded
    assert updated.superseded_by_claim_id == c2.id


def test_content_fields_not_mutable(tmp_db: duckdb.DuckDBPyConnection) -> None:
    """No general update_claim function exists — only update_claim_status."""
    from mesh_db import claims as claims_module
    assert not hasattr(claims_module, "update_claim")


def test_fk_constraint_missing_entity(tmp_db: duckdb.DuckDBPyConnection) -> None:
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
    with pytest.raises(duckdb.Error):
        create_claim(tmp_db, c)
