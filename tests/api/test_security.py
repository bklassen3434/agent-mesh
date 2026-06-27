"""Privileged-write guard (``mesh_api.security.require_internal_admin``).

The wiki is the auth boundary; the API trusts a shared internal token + admin
role header on privileged writes. When the token is configured, a write that
doesn't carry it (e.g. a browser hitting the API port directly) is rejected.
When it's unset (dev/tests), the guard no-ops so nothing changes.
"""
from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

_FIELD = {
    "name": "Test Topic",
    "description": "a test research knowledge base",
}


def test_create_field_rejected_without_token(
    empty_client: TestClient, monkeypatch: Any
) -> None:
    monkeypatch.setenv("MESH_INTERNAL_TOKEN", "s3cret")
    resp = empty_client.post("/api/v1/fields", json=_FIELD)
    assert resp.status_code == 401


def test_create_field_rejected_with_wrong_token(
    empty_client: TestClient, monkeypatch: Any
) -> None:
    monkeypatch.setenv("MESH_INTERNAL_TOKEN", "s3cret")
    resp = empty_client.post(
        "/api/v1/fields",
        json=_FIELD,
        headers={"X-Mesh-Internal-Token": "nope", "X-Mesh-Role": "admin"},
    )
    assert resp.status_code == 401


def test_create_field_rejected_when_not_admin(
    empty_client: TestClient, monkeypatch: Any
) -> None:
    monkeypatch.setenv("MESH_INTERNAL_TOKEN", "s3cret")
    resp = empty_client.post(
        "/api/v1/fields",
        json=_FIELD,
        headers={"X-Mesh-Internal-Token": "s3cret", "X-Mesh-Role": "beta"},
    )
    assert resp.status_code == 403


def test_create_field_allowed_with_token_and_admin(
    empty_client: TestClient, monkeypatch: Any
) -> None:
    monkeypatch.setenv("MESH_INTERNAL_TOKEN", "s3cret")
    resp = empty_client.post(
        "/api/v1/fields",
        json=_FIELD,
        headers={"X-Mesh-Internal-Token": "s3cret", "X-Mesh-Role": "admin"},
    )
    assert resp.status_code == 201
    assert resp.json()["slug"] == "test-topic"


def test_guard_noops_without_configured_token(empty_client: TestClient) -> None:
    # No MESH_INTERNAL_TOKEN set → writes are unguarded (dev/local posture).
    resp = empty_client.post(
        "/api/v1/fields",
        json={"name": "Unguarded Topic", "description": "a dev-mode knowledge base"},
    )
    assert resp.status_code == 201
