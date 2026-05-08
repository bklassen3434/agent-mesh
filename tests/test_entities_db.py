from __future__ import annotations

import duckdb
from mesh_db.entities import create_entity, get_entity_by_id, list_entities, update_entity
from mesh_models.entity import Entity, EntityType


def _make_entity(**kwargs: object) -> Entity:
    defaults: dict[str, object] = {"canonical_name": "TestModel", "type": EntityType.model}
    defaults.update(kwargs)
    return Entity(**defaults)  # type: ignore[arg-type]


def test_create_and_get(tmp_db: duckdb.DuckDBPyConnection) -> None:
    e = _make_entity()
    create_entity(tmp_db, e)
    fetched = get_entity_by_id(tmp_db, e.id)
    assert fetched is not None
    assert fetched.id == e.id
    assert fetched.canonical_name == e.canonical_name
    assert fetched.type == EntityType.model


def test_get_missing_returns_none(tmp_db: duckdb.DuckDBPyConnection) -> None:
    assert get_entity_by_id(tmp_db, "nonexistent-id") is None


def test_list_all(tmp_db: duckdb.DuckDBPyConnection) -> None:
    for name in ["A", "B", "C"]:
        create_entity(tmp_db, _make_entity(canonical_name=name))
    result = list_entities(tmp_db)
    assert len(result) >= 3


def test_list_filter_by_type(tmp_db: duckdb.DuckDBPyConnection) -> None:
    create_entity(tmp_db, _make_entity(canonical_name="Paper1", type=EntityType.paper))
    create_entity(tmp_db, _make_entity(canonical_name="Model1", type=EntityType.model))
    papers = list_entities(tmp_db, type=EntityType.paper)
    assert all(e.type == EntityType.paper for e in papers)


def test_aliases_round_trip(tmp_db: duckdb.DuckDBPyConnection) -> None:
    e = _make_entity(aliases=["alias-a", "alias-b"])
    create_entity(tmp_db, e)
    fetched = get_entity_by_id(tmp_db, e.id)
    assert fetched is not None
    assert set(fetched.aliases) == {"alias-a", "alias-b"}


def test_attributes_round_trip(tmp_db: duckdb.DuckDBPyConnection) -> None:
    e = _make_entity(attributes={"params": "70B", "context": "128k"})
    create_entity(tmp_db, e)
    fetched = get_entity_by_id(tmp_db, e.id)
    assert fetched is not None
    assert fetched.attributes["params"] == "70B"


def test_update_canonical_name(tmp_db: duckdb.DuckDBPyConnection) -> None:
    e = _make_entity()
    create_entity(tmp_db, e)
    updated = update_entity(tmp_db, e.id, canonical_name="NewName")
    assert updated.canonical_name == "NewName"


def test_list_limit(tmp_db: duckdb.DuckDBPyConnection) -> None:
    for i in range(10):
        create_entity(tmp_db, _make_entity(canonical_name=f"M{i}"))
    result = list_entities(tmp_db, limit=3)
    assert len(result) == 3
