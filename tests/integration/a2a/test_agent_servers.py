"""Integration tests for A2A agent servers.

Each test spawns a single agent server in a subprocess, hits it with httpx,
and asserts on Agent Card validity and skill response shape.
No docker required — CI-safe.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from collections.abc import Generator
from typing import Any

import httpx
import pytest
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from mesh_a2a.client import MeshA2AClient

# ── helpers ────────────────────────────────────────────────────────────────


def _wait_for_server(url: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=1.0)
            return
        except Exception:
            time.sleep(0.3)
    raise TimeoutError(f"Server at {url} did not start within {timeout}s")


@pytest.fixture()
def entity_tracker_server() -> Generator[str, None, None]:
    """Spawn the entity tracker A2A server on a free port."""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mesh_agent_servers.entity_tracker",
        ],
        env={
            **__import__("os").environ,
            "AGENT_HOST": "127.0.0.1",
            "AGENT_PORT": str(port),
            "AGENT_PUBLIC_URL": base_url,
        },
    )
    try:
        _wait_for_server(f"{base_url}/healthz")
        yield base_url
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.fixture()
def sota_tracker_server() -> Generator[str, None, None]:
    """Spawn the SOTA tracker A2A server on a free port."""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mesh_agent_servers.sota_tracker",
        ],
        env={
            **__import__("os").environ,
            "AGENT_HOST": "127.0.0.1",
            "AGENT_PORT": str(port),
            "AGENT_PUBLIC_URL": base_url,
        },
    )
    try:
        _wait_for_server(f"{base_url}/healthz")
        yield base_url
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# ── healthz ────────────────────────────────────────────────────────────────


def test_entity_tracker_healthz(entity_tracker_server: str) -> None:
    resp = httpx.get(f"{entity_tracker_server}/healthz")
    assert resp.status_code == 200
    assert resp.json()["agent"] == "entity_tracker"


def test_sota_tracker_healthz(sota_tracker_server: str) -> None:
    resp = httpx.get(f"{sota_tracker_server}/healthz")
    assert resp.status_code == 200
    assert resp.json()["agent"] == "sota_tracker"


# ── Agent Card validity ────────────────────────────────────────────────────


def test_entity_tracker_agent_card(entity_tracker_server: str) -> None:
    resp = httpx.get(f"{entity_tracker_server}{AGENT_CARD_WELL_KNOWN_PATH}")
    assert resp.status_code == 200
    card = resp.json()
    assert card["name"] == "Entity Tracker"
    assert any(s["id"] == "resolve_entities" for s in card["skills"])
    assert card["capabilities"]["streaming"] is False


def test_sota_tracker_agent_card(sota_tracker_server: str) -> None:
    resp = httpx.get(f"{sota_tracker_server}{AGENT_CARD_WELL_KNOWN_PATH}")
    assert resp.status_code == 200
    card = resp.json()
    assert card["name"] == "SOTA Tracker"
    assert any(s["id"] == "update_sota" for s in card["skills"])


# ── Skill dispatch via MeshA2AClient ──────────────────────────────────────


def test_entity_tracker_skill_new_entity(entity_tracker_server: str) -> None:
    async def _run() -> dict[str, Any]:
        async with MeshA2AClient() as c:
            await c.discover([entity_tracker_server])
            return await c.call_skill(
                "resolve_entities",
                {"candidate_names": ["GPT-4"], "existing_entities": []},
            )

    result = asyncio.run(_run())
    resolved = result["resolved"]
    assert len(resolved) == 1
    assert resolved[0]["name"] == "GPT-4"
    assert resolved[0]["is_new"] is True
    assert resolved[0]["entity_id"]  # non-empty UUID string


def test_entity_tracker_skill_existing_entity(entity_tracker_server: str) -> None:
    existing = [
        {
            "entity_id": "existing-uuid-123",
            "canonical_name": "BERT",
            "aliases": ["bert-base"],
            "entity_type": "model",
        }
    ]

    async def _run() -> dict[str, Any]:
        async with MeshA2AClient() as c:
            await c.discover([entity_tracker_server])
            return await c.call_skill(
                "resolve_entities",
                {"candidate_names": ["bert-base"], "existing_entities": existing},
            )

    result = asyncio.run(_run())
    resolved = result["resolved"]
    assert len(resolved) == 1
    assert resolved[0]["entity_id"] == "existing-uuid-123"
    assert resolved[0]["is_new"] is False


def test_sota_tracker_skill_new_belief(sota_tracker_server: str) -> None:
    claims = [
        {
            "claim_id": "c1",
            "subject_entity_id": "e1",
            "predicate": "achieves_score",
            "object": {"score": 87.5, "benchmark": "MMLU", "metric": "accuracy"},
            "source_id": "s1",
            "raw_excerpt": "achieves 87.5 on MMLU",
            "confidence": 0.9,
        }
    ]

    async def _run() -> dict[str, Any]:
        async with MeshA2AClient() as c:
            await c.discover([sota_tracker_server])
            return await c.call_skill(
                "update_sota",
                {"claims": claims, "existing_sota_beliefs": []},
            )

    result = asyncio.run(_run())
    updates = result["belief_updates"]
    assert len(updates) == 1
    assert updates[0]["topic"] == "sota:MMLU"
    assert updates[0]["is_new_belief"] is True


def test_sota_tracker_skill_better_score_revision(sota_tracker_server: str) -> None:
    existing_beliefs = [
        {
            "belief_id": "b1",
            "topic": "sota:MMLU",
            "statement": "OldModel achieves 80.0 accuracy on MMLU (as of 2024-01-01)",
            "confidence": 0.5,
        }
    ]
    claims = [
        {
            "claim_id": "c2",
            "subject_entity_id": "e2",
            "predicate": "achieves_score",
            "object": {"score": 92.0, "benchmark": "MMLU", "metric": "accuracy"},
            "source_id": "s1",
            "raw_excerpt": "achieves 92.0 on MMLU",
            "confidence": 0.9,
        }
    ]

    async def _run() -> dict[str, Any]:
        async with MeshA2AClient() as c:
            await c.discover([sota_tracker_server])
            return await c.call_skill(
                "update_sota",
                {"claims": claims, "existing_sota_beliefs": existing_beliefs},
            )

    result = asyncio.run(_run())
    updates = result["belief_updates"]
    assert len(updates) == 1
    assert updates[0]["is_new_belief"] is False
    assert updates[0]["existing_belief_id"] == "b1"


def test_discovery_builds_skill_map(
    entity_tracker_server: str, sota_tracker_server: str
) -> None:
    async def _run() -> dict[str, str]:
        async with MeshA2AClient() as c:
            await c.discover([entity_tracker_server, sota_tracker_server])
            return c.skill_map()

    skill_map = asyncio.run(_run())
    assert "resolve_entities" in skill_map
    assert "update_sota" in skill_map
