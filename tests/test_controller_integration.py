"""End-to-end check of the deterministic controller with the *real* skills.

Where the per-skill tests drive one skill in isolation and ``test_controller.py``
drives the loop with a fake skill, this wires the whole machine together: it
seeds a small board, registers the actual built-in skills via
``load_builtin_skills`` (the production startup path), and runs ``run_controller``.

The board is seeded so the rules surface one activation for each of the three
things the migration calls out — an unread source, a thin belief, and a
duplicate-looking entity pair — which between them exercise every skill class.

No LLM is reachable in the test environment (the api key is removed), so the
LLM-bound skills degrade exactly as in production (caught by the controller's
per-skill guard or by their own fallbacks), and the *rule-based* paths
(``merge-candidate`` high band, ``investigate-gap`` fallback) still produce real
effects. The headline assertions are controller-level: every planned activation
has a registered skill (nothing ``skipped_no_skill``), the controller dispatches
the work, and it produces effects — first in shadow (previewed, never written),
then live (materialised through the write gateway).
"""
from __future__ import annotations

import asyncio
import importlib
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from mesh_agents.skill import clear_registry, load_builtin_skills
from mesh_db.connection import MeshConnection
from mesh_db.entities import count_entities, create_entity, set_entity_embedding
from mesh_db.investigations import list_investigations
from mesh_db.sources import create_source
from mesh_models.belief import Belief
from mesh_models.entity import Entity, EntityType
from mesh_models.field import DEFAULT_FIELD_ID, DEFAULT_FIELD_SLUG
from mesh_models.source import Source, SourceType
from mesh_pipeline.controller import run_controller

# The built-in skill modules load_builtin_skills() imports. We reload them on a
# clean registry so their @register_skill decorators re-run deterministically.
_SKILL_MODULES = (
    "mesh_agents.skills.extract_source",
    "mesh_agents.skills.merge_candidate",
    "mesh_agents.skills.consolidate_beliefs",
    "mesh_agents.skills.synthesize_belief",
    "mesh_agents.skills.challenge_belief",
    "mesh_agents.skills.investigate_gap",
    "mesh_agents.skills.maintain_belief",
    "mesh_agents.skills.consolidate_memory",
    "mesh_agents.skills.scout_source",
    "mesh_agents.skills.write_field_brief",
)


@pytest.fixture(autouse=True)
def _no_network_scout(tmp_db: MeshConnection) -> Any:
    """Disable connectors so the scout-when-idle rule doesn't fire a real network
    scout. Connector enable-state lives in the ``catalog`` schema, which conftest
    does NOT truncate between tests — so restore it on teardown to avoid leaking
    the disable into every later test."""
    from mesh_db.connectors import enable_connector, list_field_connectors

    prior = [
        (fc.connector_id, fc.config)
        for fc in list_field_connectors(tmp_db, DEFAULT_FIELD_ID, enabled_only=True)
    ]
    for cid, config in prior:
        enable_connector(tmp_db, DEFAULT_FIELD_ID, cid, config=config, enabled=False)
    yield
    for cid, config in prior:
        enable_connector(tmp_db, DEFAULT_FIELD_ID, cid, config=config, enabled=True)


def _register_real_skills() -> list[str]:
    """Populate the registry with the genuine built-in skills and return their ids.

    Import each module first (a no-op if already loaded), *then* clear the registry
    and reload — so each module body executes exactly once after the clear and
    @register_skill never sees a duplicate."""
    modules = [importlib.import_module(name) for name in _SKILL_MODULES]
    clear_registry()
    for module in modules:
        importlib.reload(module)
    return [s.skill_id for s in load_builtin_skills()]


@pytest.fixture
def real_skills(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Register the real skills and keep the environment LLM-free + routing-off so
    the LLM-bound skills degrade deterministically instead of hitting the network."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MESH_ROUTE_ENABLED", raising=False)
    ids = _register_real_skills()
    try:
        yield ids
    finally:
        _register_real_skills()


def _unit_vec() -> list[float]:
    """A 384-dim unit vector. Two entities sharing it embed at cosine 1.0 → the
    merge-candidate skill's high band auto-merges without an LLM."""
    return [1.0] + [0.0] * 383


def _seed_board(conn: MeshConnection) -> dict[str, Any]:
    """Seed the three things the migration names: an unread source, a thin belief,
    and a duplicate-looking entity pair (connectors are disabled by the autouse
    fixture)."""
    source = Source(
        type=SourceType.arxiv,
        url="https://arxiv.org/abs/2406.00001",
        published_at=datetime(2026, 6, 13, tzinfo=UTC),
        raw_content_hash="hash-controller-integration",
    )
    create_source(conn, source, field_id=DEFAULT_FIELD_ID)

    belief = Belief(
        topic="capability:flownet",
        statement="FlowNet reaches strong optical-flow accuracy.",
        supporting_claim_ids=[],
        confidence=0.4,
        is_currently_held=True,
    )
    from mesh_db.beliefs import create_belief

    create_belief(conn, belief, field_id=DEFAULT_FIELD_ID)

    ent_a = Entity(canonical_name="FlowNet", type=EntityType.model)
    ent_b = Entity(canonical_name="FlowNet (v2)", type=EntityType.model)
    create_entity(conn, ent_a, field_id=DEFAULT_FIELD_ID)
    create_entity(conn, ent_b, field_id=DEFAULT_FIELD_ID)
    set_entity_embedding(conn, ent_a.id, _unit_vec())
    set_entity_embedding(conn, ent_b.id, _unit_vec())

    return {
        "source_id": source.id,
        "belief_id": belief.id,
        "entity_ids": [ent_a.id, ent_b.id],
    }


def test_controller_shadow_plans_and_handles_every_tension(
    real_skills: list[str], tmp_db: MeshConnection
) -> None:
    """Shadow round: the real skills cover every planned activation, the controller
    dispatches the work and previews effects — and writes nothing."""
    assert {
        "extract-source",
        "merge-candidate",
        "consolidate-beliefs",
        "synthesize-belief",
        "challenge-belief",
        "investigate-gap",
    } <= set(real_skills)

    _seed_board(tmp_db)
    entities_before = count_entities(tmp_db, field_id=DEFAULT_FIELD_ID)
    investigations_before = len(list_investigations(tmp_db, field_id=DEFAULT_FIELD_ID))

    result = asyncio.run(run_controller(DEFAULT_FIELD_SLUG, shadow=True, conn=tmp_db))

    assert len(result.rounds) == 1
    r0 = result.rounds[0]

    # The board surfaced our three seeds (+ the two under-evidenced dup entities).
    assert r0.candidates >= 3
    # Every planned activation has a registered skill — nothing falls through.
    assert r0.skipped_no_skill == 0
    assert r0.dispatched >= 3
    assert r0.effects >= 1

    # Shadow writes nothing: no apply report, and the board is byte-for-byte intact.
    assert r0.apply is None
    assert count_entities(tmp_db, field_id=DEFAULT_FIELD_ID) == entities_before
    assert len(list_investigations(tmp_db, field_id=DEFAULT_FIELD_ID)) == (
        investigations_before
    )


def test_controller_live_materialises_effects_through_gateway(
    real_skills: list[str], tmp_db: MeshConnection
) -> None:
    """One live round: the same skills' effects flow through the write gateway and
    actually change the board — the merge-candidate and investigate-gap rule-based
    paths land without an LLM."""
    _seed_board(tmp_db)
    entities_before = count_entities(tmp_db, field_id=DEFAULT_FIELD_ID)

    result = asyncio.run(
        run_controller(DEFAULT_FIELD_SLUG, shadow=False, max_rounds=1, conn=tmp_db)
    )

    applied = [r.apply for r in result.rounds if r.apply is not None]
    assert applied, "live mode should have applied effects through the gateway"
    report = applied[0]

    # The duplicate pair was merged and gap investigations were opened — both
    # rule-based, so they land with no LLM reachable.
    assert report.entities_merged >= 1
    assert report.investigations_opened >= 1

    # The board really changed: one fewer entity, at least one investigation row.
    assert count_entities(tmp_db, field_id=DEFAULT_FIELD_ID) == entities_before - 1
    assert len(list_investigations(tmp_db, field_id=DEFAULT_FIELD_ID)) >= 1
