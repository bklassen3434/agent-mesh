"""The deterministic controller loop (auction-free).

Drives ``run_controller`` with a registered fake skill on the test container: a
shadow round previews the plan + effects without writing; a live round dispatches
the planned activations, runs the skill, applies its effects through the gateway,
and records per-tension dispatch counters. Also proves the safe quiescent
behaviour on an empty board.

Where ``test_rules.py`` tests the pure rule engine (plan() over hand-built state),
this exercises the whole loop end-to-end against Postgres.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mesh_agents.skill import clear_registry, register_skill
from mesh_db.beliefs import list_beliefs
from mesh_db.connection import MeshConnection
from mesh_db.sources import create_source
from mesh_models.belief import Belief
from mesh_models.effect import CreateBeliefEffect
from mesh_models.source import Source, SourceType
from mesh_models.tension import TensionKind
from mesh_pipeline.controller import run_controller

_FIELD = "ai-robotics"


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    clear_registry()
    yield
    clear_registry()


@pytest.fixture(autouse=True)
def _no_network_scout(tmp_db: MeshConnection) -> Any:
    """The seeded field enables every connector; disable them so the controller's
    scout-when-idle rule doesn't fire a real network scout (scouting is covered by
    test_skill_scout_source with a stubbed handler).

    Connector enable-state lives in the ``catalog`` schema, which conftest does
    NOT truncate between tests — so snapshot the prior state and restore it on
    teardown, or this disable would leak into every later test sharing the DB."""
    from mesh_db.connectors import enable_connector, list_field_connectors

    prior = [
        (fc.connector_id, fc.config)
        for fc in list_field_connectors(tmp_db, _FIELD, enabled_only=True)
    ]
    for cid, config in prior:
        enable_connector(tmp_db, _FIELD, cid, config=config, enabled=False)
    yield
    for cid, config in prior:
        enable_connector(tmp_db, _FIELD, cid, config=config, enabled=True)


def _unread_source(conn: MeshConnection, tag: str) -> Source:
    from datetime import UTC, datetime

    return create_source(
        conn,
        Source(
            type=SourceType.arxiv,
            url=f"https://example.com/{tag}",
            published_at=datetime(2026, 6, 13, tzinfo=UTC),
            raw_content_hash=f"hash-{tag}",
        ),
    )


def _register_belief_maker() -> None:
    @register_skill
    class _BeliefMaker:
        skill_id = "extract-source"  # the handler the unread-source tension names
        handles = (TensionKind.unextracted_source,)

        async def run(self, conn: Any, tension: Any, *, budget_usd: float) -> list[Any]:
            return [
                CreateBeliefEffect(
                    field_id=tension.field_id,
                    belief=Belief(
                        topic=f"from-{tension.target_ref.get('source_id', 'x')[:6]}",
                        statement="controller-produced belief",
                        confidence=0.5,
                        is_currently_held=True,
                    ),
                )
            ]


def test_controller_on_empty_board_is_quiescent(tmp_db: MeshConnection) -> None:
    # Nothing to scout (connectors disabled) and nothing on the board → no rule
    # fires, the plan is empty, and the controller reports quiescence without
    # writing or recording a round.
    result = asyncio.run(run_controller(_FIELD, shadow=True, conn=tmp_db))
    assert result.quiescent is True
    assert result.rounds == []


def test_shadow_round_previews_plan_without_writing(tmp_db: MeshConnection) -> None:
    _register_belief_maker()
    _unread_source(tmp_db, "shadow-src")
    before = len(list_beliefs(tmp_db, currently_held=True, limit=1000))

    result = asyncio.run(run_controller(_FIELD, shadow=True, conn=tmp_db))

    assert len(result.rounds) == 1
    assert result.rounds[0].planned >= 1
    assert result.rounds[0].dispatched >= 1
    assert result.rounds[0].effects >= 1
    assert result.rounds[0].apply is None  # shadow: nothing applied
    # The board is unchanged — no belief was written.
    after = len(list_beliefs(tmp_db, currently_held=True, limit=1000))
    assert after == before


def test_live_round_applies_effects_through_gateway(tmp_db: MeshConnection) -> None:
    _register_belief_maker()
    _unread_source(tmp_db, "live-src")
    before = len(list_beliefs(tmp_db, currently_held=True, limit=1000))

    result = asyncio.run(
        run_controller(_FIELD, shadow=False, max_rounds=1, conn=tmp_db)
    )

    applied = [r.apply for r in result.rounds if r.apply is not None]
    assert applied and sum(a.beliefs_created for a in applied) >= 1
    after = len(list_beliefs(tmp_db, currently_held=True, limit=1000))
    assert after > before


def test_live_run_records_a_controller_pipeline_run(tmp_db: MeshConnection) -> None:
    from mesh_db.pipeline_runs import pipeline_run_exists

    _register_belief_maker()
    _unread_source(tmp_db, "ledger-src")
    result = asyncio.run(run_controller(_FIELD, shadow=False, max_rounds=1, conn=tmp_db))
    # The run is on the ledger (visible to /status + pipeline-stats), typed controller.
    assert pipeline_run_exists(tmp_db, result.run_id)
    row = tmp_db.execute(
        "SELECT run_type, beliefs_created FROM pipeline_runs WHERE id = %s",
        [result.run_id],
    ).fetchone()
    assert row is not None
    assert row[0] == "controller"
    assert row[1] >= 1  # the fake skill's belief was created


def test_shadow_run_records_no_pipeline_run(tmp_db: MeshConnection) -> None:
    from mesh_db.pipeline_runs import pipeline_run_exists

    _register_belief_maker()
    _unread_source(tmp_db, "shadow-ledger")
    result = asyncio.run(run_controller(_FIELD, shadow=True, conn=tmp_db))
    assert not pipeline_run_exists(tmp_db, result.run_id)


def test_step_cap_limits_dispatch_per_round(tmp_db: MeshConnection) -> None:
    _register_belief_maker()
    for i in range(10):
        _unread_source(tmp_db, f"cap-{i}")
    # 10 unread-source tensions, a step cap of 2 → at most 2 dispatched this round.
    result = asyncio.run(
        run_controller(_FIELD, shadow=False, max_rounds=1, step_cap=2, conn=tmp_db)
    )
    assert result.rounds
    assert result.rounds[0].dispatched == 2
    assert result.rounds[0].planned >= 10  # the rest wait for a later round


def test_dispatch_records_tension_counters(tmp_db: MeshConnection) -> None:
    from mesh_db.controller_state import DispatchOutcome, get_tension_states

    _register_belief_maker()
    src = _unread_source(tmp_db, "counter-src")
    asyncio.run(run_controller(_FIELD, shadow=False, max_rounds=1, conn=tmp_db))

    states = get_tension_states(tmp_db, tmp_db_field_id(tmp_db))
    tid = f"{TensionKind.unextracted_source.value}:{src.id}"
    assert tid in states
    st = states[tid]
    assert st.attempts == 1
    assert st.last_outcome == DispatchOutcome.effects  # the fake skill emitted one
    assert st.last_effect_count == 1


def tmp_db_field_id(conn: MeshConnection) -> str:
    from mesh_db.fields import get_field_by_slug

    field = get_field_by_slug(conn, _FIELD)
    assert field is not None
    return field.id
