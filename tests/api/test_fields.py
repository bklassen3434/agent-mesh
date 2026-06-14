"""Phase 18 UX surface — field onboarding endpoints.

The ``fields`` table is not truncated between tests (it's seeded once), so each
create test uses a distinct slug to avoid cross-test 409s within a session.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_list_includes_default(client: TestClient) -> None:
    res = client.get("/api/v1/fields")
    assert res.status_code == 200
    slugs = {f["slug"] for f in res.json()}
    assert "ai-robotics" in slugs


def test_get_default_field(client: TestClient) -> None:
    res = client.get("/api/v1/fields/ai-robotics")
    assert res.status_code == 200
    body = res.json()
    assert body["slug"] == "ai-robotics"
    assert body["profile"]["description"] == "an AI/robotics research knowledge base"


def test_get_unknown_field_404(client: TestClient) -> None:
    assert client.get("/api/v1/fields/nope-nope").status_code == 404


def test_create_field_slugifies_and_seeds_profile(client: TestClient) -> None:
    res = client.post(
        "/api/v1/fields",
        json={
            "name": "Materials Science",
            "description": "a materials-science research knowledge base",
            "entity_type_hints": ["MOF-5", "Perovskite"],
            "topic_label": "frontier",
        },
    )
    assert res.status_code == 201
    body = res.json()
    assert body["slug"] == "materials-science"
    assert body["id"] == "materials-science"
    assert body["is_active"] is True
    assert body["profile"]["entity_type_hints"] == ["MOF-5", "Perovskite"]
    assert body["profile"]["topic_label"] == "frontier"
    # visible via GET
    got = client.get("/api/v1/fields/materials-science")
    assert got.status_code == 200
    assert got.json()["name"] == "Materials Science"


def test_create_duplicate_409(client: TestClient) -> None:
    payload = {"name": "Quantum Computing", "description": "a quantum-computing knowledge base"}
    assert client.post("/api/v1/fields", json=payload).status_code == 201
    assert client.post("/api/v1/fields", json=payload).status_code == 409


def test_create_empty_name_422(client: TestClient) -> None:
    res = client.post("/api/v1/fields", json={"name": "  ", "description": "x"})
    assert res.status_code == 422


def test_create_name_without_alnum_422(client: TestClient) -> None:
    res = client.post("/api/v1/fields", json={"name": "!!!", "description": "x"})
    assert res.status_code == 422


def test_create_empty_description_422(client: TestClient) -> None:
    res = client.post("/api/v1/fields", json={"name": "Genomics", "description": "   "})
    assert res.status_code == 422


def test_patch_updates_profile_and_active(client: TestClient) -> None:
    client.post(
        "/api/v1/fields",
        json={"name": "Climate Models", "description": "a climate-modeling knowledge base"},
    )
    res = client.patch(
        "/api/v1/fields/climate-models",
        json={"description": "a climate science knowledge base", "is_active": False},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["profile"]["description"] == "a climate science knowledge base"
    assert body["is_active"] is False
    # slug stays immutable
    assert body["slug"] == "climate-models"
    # inactive fields are hidden from active_only listing but still gettable
    listed = client.get("/api/v1/fields", params={"active_only": "true"}).json()
    assert "climate-models" not in {f["slug"] for f in listed}
    assert client.get("/api/v1/fields/climate-models").status_code == 200


def test_patch_unknown_field_404(client: TestClient) -> None:
    res = client.patch("/api/v1/fields/ghost", json={"is_active": False})
    assert res.status_code == 404
