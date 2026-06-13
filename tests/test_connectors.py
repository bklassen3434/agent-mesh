"""Phase 17c — connector catalog + per-field enablement."""
from __future__ import annotations

import pytest
from mesh_db.connection import MeshConnection
from mesh_db.connectors import (
    enable_connector,
    get_connector,
    list_connectors,
    list_field_connectors,
)
from mesh_db.fields import create_field
from mesh_models.field import DEFAULT_FIELD_ID, Field, FieldProfile


def test_catalog_seeded_with_builtins(tmp_db: MeshConnection) -> None:
    connectors = list_connectors(tmp_db)
    slugs = {c.slug for c in connectors}
    assert {"arxiv", "hn", "github", "bluesky", "reddit", "blog", "leaderboard"} <= slugs
    arxiv = get_connector(tmp_db, "arxiv")
    assert arxiv is not None
    assert arxiv.scout_skill_id == "scout_arxiv"
    assert "categories" in arxiv.config_schema


def test_ai_robotics_enablement_matches_defaults(tmp_db: MeshConnection) -> None:
    enabled = list_field_connectors(tmp_db, DEFAULT_FIELD_ID, enabled_only=True)
    by_id = {fc.connector_id: fc for fc in enabled}
    assert by_id["arxiv"].config["categories"] == ["cs.AI", "cs.RO", "cs.LG"]
    assert by_id["hn"].config["keywords"][0] == "AI"
    assert by_id["github"].config["topics"] == [
        "llm", "agents", "machine-learning", "ai", "robotics"
    ]
    # every built-in is enabled for the seeded field
    assert len(enabled) == 7


def _other_field(conn: MeshConnection) -> str:
    from mesh_db.fields import get_field_by_slug

    if get_field_by_slug(conn, "agri") is None:
        create_field(
            conn,
            Field(id="agri", name="Agri", slug="agri",
                  profile=FieldProfile(slug="agri", name="Agri", description="farming")),
        )
    return "agri"


def test_enable_validates_config(tmp_db: MeshConnection) -> None:
    field_id = _other_field(tmp_db)
    # good config persists
    fc = enable_connector(
        tmp_db, field_id, "arxiv", config={"categories": ["physics.gen-ph"]}
    )
    assert fc.config["categories"] == ["physics.gen-ph"]
    roundtrip = list_field_connectors(tmp_db, field_id, enabled_only=True)
    assert any(
        c.connector_id == "arxiv" and c.config["categories"] == ["physics.gen-ph"]
        for c in roundtrip
    )
    # wrong type rejected
    with pytest.raises(ValueError):
        enable_connector(tmp_db, field_id, "arxiv", config={"categories": "not-a-list"})
    # unknown key rejected
    with pytest.raises(ValueError):
        enable_connector(tmp_db, field_id, "hn", config={"bogus": 1})
    # missing required key rejected (arxiv.categories is required)
    with pytest.raises(ValueError):
        enable_connector(tmp_db, field_id, "arxiv", config={})


def test_unknown_connector_rejected(tmp_db: MeshConnection) -> None:
    field_id = _other_field(tmp_db)
    with pytest.raises(ValueError):
        enable_connector(tmp_db, field_id, "does-not-exist", config={})


# ── Phase 18a: config-driven connectors ──────────────────────────────────────


def test_config_driven_connectors_in_catalog_only(tmp_db: MeshConnection) -> None:
    from mesh_models.connector import ConnectorKind

    slugs = {c.slug for c in list_connectors(tmp_db)}
    assert {"web_search", "rss", "rest_json"} <= slugs
    web = get_connector(tmp_db, "web_search")
    assert web is not None
    assert web.kind == ConnectorKind.config_driven
    assert web.scout_skill_id == "scout_web_search"
    # NOT seeded into ai-robotics — that field keeps its built-in scouts.
    ai_ids = {fc.connector_id for fc in list_field_connectors(tmp_db, DEFAULT_FIELD_ID)}
    assert not ({"web_search", "rss", "rest_json"} & ai_ids)


def test_enable_config_driven_on_new_field(tmp_db: MeshConnection) -> None:
    field_id = _other_field(tmp_db)
    fc = enable_connector(
        tmp_db, field_id, "web_search",
        config={"web_seed_queries": ["precision agriculture sensors"]},
    )
    assert fc.config["web_seed_queries"] == ["precision agriculture sensors"]
    # rss requires feed_url
    with pytest.raises(ValueError):
        enable_connector(tmp_db, field_id, "rss", config={})
    enable_connector(
        tmp_db, field_id, "rss",
        config={"feed_url": "https://example.com/feed", "include_terms": ["crop"]},
    )
    enabled = {fc.connector_id for fc in list_field_connectors(tmp_db, field_id, enabled_only=True)}
    assert {"web_search", "rss"} <= enabled
