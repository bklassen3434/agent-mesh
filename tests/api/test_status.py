"""Phase 6b.6 tests for the /status route.

The route renders HTML — these tests assert on substrings and basic
shape rather than full DOM parsing. The point is "panels render, no
500s, expected data shows up", not pixel-perfect markup.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mesh_db.connection import get_connection


def test_status_renders_with_empty_db(empty_client: TestClient) -> None:
    resp = empty_client.get("/status")
    assert resp.status_code == 200
    body = resp.text
    assert "Mesh status" in body
    # meta-refresh is what keeps it live without JS.
    assert 'http-equiv="refresh"' in body
    # Empty DB → "never" for last runs, zeros for counts.
    assert "never" in body
    assert "Claims" in body and "Beliefs" in body and "Sources" in body
    # Run-errors panel (from LangGraph checkpoints) is empty without a
    # checkpoint store configured in tests.
    assert "No errors recorded" in body
    assert "Runs — checkpointed" in body


def test_status_shows_recent_runs(empty_client: TestClient, empty_db_path: Path) -> None:
    # Seed a completed pipeline run; the run panels still read pipeline_runs.
    from mesh_db.pipeline_runs import PipelineRun, create_pipeline_run

    conn = get_connection(read_only=False)
    try:
        run = PipelineRun(
            run_type="controller",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            triggered_by="scheduled",
            claims_inserted=7,
            beliefs_created=2,
            beliefs_revised=1,
        )
        create_pipeline_run(conn, run)
    finally:
        conn.close()

    resp = empty_client.get("/status")
    assert resp.status_code == 200
    body = resp.text
    # Pipeline panel reflects deltas + triggered_by + dur (0s since
    # finished_at == started_at in the fixture).
    assert "+7 claims" in body
    assert "+2 / ~1 beliefs" in body
    assert "scheduled" in body


def test_status_langfuse_section_when_unconfigured(
    empty_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    resp = empty_client.get("/status")
    assert resp.status_code == 200
    assert "Langfuse not configured" in resp.text


def test_status_langfuse_section_when_configured_but_unreachable(
    empty_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point at a host that won't answer in 2s — the route should
    # gracefully degrade to the "unavailable" branch.
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:1")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    resp = empty_client.get("/status")
    assert resp.status_code == 200
    assert "Langfuse" in resp.text
    # Either "unavailable" message or the success path with a number —
    # both are acceptable; the contract is "no 500."
    assert "open Langfuse" in resp.text or "unavailable" in resp.text
