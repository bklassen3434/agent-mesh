"""Phase 18 UX surface — connector catalog + per-field enablement endpoints.

Reads exercise the seeded catalog and the default field's enablement; the write
path targets a throwaway field so it never perturbs the ai-robotics row count
(which test_connectors.py asserts is exactly 7).
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from mesh_db.connection import get_connection
from mesh_db.fields import create_field, get_field_by_slug
from mesh_models.field import Field, FieldProfile


@pytest.fixture
def scratch_field() -> Iterator[str]:
    """A throwaway field with no connectors enabled, for write tests."""
    slug = "connector-test"
    conn = get_connection(read_only=False)
    try:
        if get_field_by_slug(conn, slug) is None:
            create_field(
                conn,
                Field(
                    id=slug,
                    name="Connector Test",
                    slug=slug,
                    profile=FieldProfile(slug=slug, name="Connector Test", description="x"),
                ),
            )
            conn.commit()
    finally:
        conn.close()
    yield slug


def test_list_catalog(client: TestClient) -> None:
    res = client.get("/api/v1/connectors")
    assert res.status_code == 200
    slugs = {c["slug"] for c in res.json()}
    # built-ins + the Phase 18 config-driven kinds
    assert {"arxiv", "github"} <= slugs
    assert {"web_search", "rss", "rest_json"} <= slugs
    arxiv = next(c for c in res.json() if c["slug"] == "arxiv")
    assert "categories" in arxiv["config_schema"]


def test_list_field_connectors_default(client: TestClient) -> None:
    res = client.get("/api/v1/fields/ai-robotics/connectors")
    assert res.status_code == 200
    by_id = {fc["connector_id"]: fc for fc in res.json()}
    assert by_id["arxiv"]["enabled"] is True
    assert by_id["arxiv"]["config"]["categories"] == ["cs.AI", "cs.RO", "cs.LG"]


def test_list_field_connectors_unknown_field_404(client: TestClient) -> None:
    res = client.get("/api/v1/fields/does-not-exist/connectors")
    assert res.status_code == 404


def test_enable_connector_with_config(client: TestClient, scratch_field: str) -> None:
    res = client.put(
        f"/api/v1/fields/{scratch_field}/connectors/web_search",
        json={"config": {"web_seed_queries": ["humanoid robots", "RL"]}, "enabled": True},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is True
    assert body["config"]["web_seed_queries"] == ["humanoid robots", "RL"]
    # round-trips through a fresh GET
    listed = client.get(f"/api/v1/fields/{scratch_field}/connectors").json()
    ws = next(fc for fc in listed if fc["connector_id"] == "web_search")
    assert ws["enabled"] is True


def test_enable_connector_bad_config_422(client: TestClient, scratch_field: str) -> None:
    # web_seed_queries must be list[str]; a bare string is rejected at write time.
    res = client.put(
        f"/api/v1/fields/{scratch_field}/connectors/web_search",
        json={"config": {"web_seed_queries": "not-a-list"}, "enabled": True},
    )
    assert res.status_code == 422


def test_enable_connector_missing_required_422(client: TestClient, scratch_field: str) -> None:
    res = client.put(
        f"/api/v1/fields/{scratch_field}/connectors/rss",
        json={"config": {}, "enabled": True},  # feed_url is required
    )
    assert res.status_code == 422


def test_enable_unknown_connector_404(client: TestClient, scratch_field: str) -> None:
    res = client.put(
        f"/api/v1/fields/{scratch_field}/connectors/nope",
        json={"config": {}, "enabled": False},
    )
    assert res.status_code == 404
