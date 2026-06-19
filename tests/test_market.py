"""Phase 1c of the agentic migration: the market loop.

Drives ``run_market`` with a registered fake skill on the test container: a
shadow round previews effects without writing; a live round funds bids, runs the
skill, and applies its effects through the gateway. Also proves the safe no-op
behaviour when no skill handles the board (the pre-Phase-2 state).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mesh_agents.skill import Bid, clear_registry, register_skill
from mesh_db.beliefs import list_beliefs
from mesh_db.connection import MeshConnection
from mesh_db.sources import create_source
from mesh_models.belief import Belief
from mesh_models.effect import CreateBeliefEffect
from mesh_models.source import Source, SourceType
from mesh_models.tension import Tension, TensionKind
from mesh_pipeline.market import run_market

_FIELD = "ai-robotics"


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    clear_registry()
    yield
    clear_registry()


@pytest.fixture(autouse=True)
def _no_network_scout(tmp_db: MeshConnection) -> None:
    """The seeded field enables every connector; disable them so run_market's
    source-acquisition tensions don't fire a real network scout (scouting is
    covered by test_skill_scout_source with a stubbed handler)."""
    from mesh_db.connectors import enable_connector, list_field_connectors

    for fc in list_field_connectors(tmp_db, _FIELD, enabled_only=True):
        enable_connector(tmp_db, _FIELD, fc.connector_id, config=fc.config, enabled=False)


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
        skill_id = "test-belief-maker"
        handles = (TensionKind.unextracted_source,)

        def bid(self, conn: Any, tension: Tension) -> Bid | None:
            return Bid(value=tension.value, est_cost_usd=tension.est_cost_usd)

        async def run(self, conn: Any, tension: Tension, *, budget_usd: float) -> list[Any]:
            return [
                CreateBeliefEffect(
                    field_id=tension.field_id,
                    belief=Belief(
                        topic=f"from-{tension.target_ref.get('source_id', 'x')[:6]}",
                        statement="market-produced belief",
                        confidence=0.5,
                        is_currently_held=True,
                    ),
                )
            ]


def test_market_on_empty_board_is_quiescent(tmp_db: MeshConnection) -> None:
    # Nothing to scout (connectors disabled) and nothing on the board → the market
    # has no tension to fund and reports quiescence without writing.
    result = asyncio.run(run_market(_FIELD, shadow=True, conn=tmp_db))
    assert result.quiescent is True
    assert all(r.funded == 0 for r in result.rounds)


def test_shadow_round_previews_effects_without_writing(tmp_db: MeshConnection) -> None:
    _register_belief_maker()
    _unread_source(tmp_db, "shadow-src")
    before = len(list_beliefs(tmp_db, currently_held=True, limit=1000))

    result = asyncio.run(run_market(_FIELD, shadow=True, conn=tmp_db))

    assert len(result.rounds) == 1
    assert result.rounds[0].funded >= 1
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
        run_market(_FIELD, shadow=False, budget_usd=0.50, max_rounds=1, conn=tmp_db)
    )

    assert result.spent_usd > 0
    applied = [r.apply for r in result.rounds if r.apply is not None]
    assert applied and sum(a.beliefs_created for a in applied) >= 1
    after = len(list_beliefs(tmp_db, currently_held=True, limit=1000))
    assert after > before


def test_live_run_records_a_market_pipeline_run(tmp_db: MeshConnection) -> None:
    from mesh_db.pipeline_runs import pipeline_run_exists

    _register_belief_maker()
    _unread_source(tmp_db, "ledger-src")
    result = asyncio.run(
        run_market(_FIELD, shadow=False, max_rounds=1, conn=tmp_db)
    )
    # The run is on the ledger (visible to /status + pipeline-stats), typed market.
    assert pipeline_run_exists(tmp_db, result.run_id)
    row = tmp_db.execute(
        "SELECT run_type, beliefs_created FROM pipeline_runs WHERE id = %s",
        [result.run_id],
    ).fetchone()
    assert row is not None
    assert row[0] == "market"
    assert row[1] >= 1  # the fake skill's belief was created


def test_shadow_run_records_no_pipeline_run(tmp_db: MeshConnection) -> None:
    from mesh_db.pipeline_runs import pipeline_run_exists

    _register_belief_maker()
    _unread_source(tmp_db, "shadow-ledger")
    result = asyncio.run(run_market(_FIELD, shadow=True, conn=tmp_db))
    assert not pipeline_run_exists(tmp_db, result.run_id)


def test_budget_caps_spend(tmp_db: MeshConnection) -> None:
    _register_belief_maker()
    for i in range(10):
        _unread_source(tmp_db, f"cap-{i}")
    # Each unread-source bid costs ~0.008; a $0.02 budget funds only a couple.
    result = asyncio.run(
        run_market(_FIELD, shadow=False, budget_usd=0.02, conn=tmp_db)
    )
    assert result.spent_usd <= 0.02 + 1e-9
