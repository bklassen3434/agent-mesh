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


def test_market_with_no_skills_is_a_safe_noop(tmp_db: MeshConnection) -> None:
    _unread_source(tmp_db, "lonely")
    result = asyncio.run(run_market(_FIELD, shadow=True, conn=tmp_db))
    # One round scanned the board, found a candidate, but no skill handled it.
    assert result.rounds
    r0 = result.rounds[0]
    assert r0.candidates >= 1
    assert r0.funded == 0
    assert r0.skipped_no_skill == r0.candidates
    assert result.quiescent is True


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


def test_budget_caps_spend(tmp_db: MeshConnection) -> None:
    _register_belief_maker()
    for i in range(10):
        _unread_source(tmp_db, f"cap-{i}")
    # Each unread-source bid costs ~0.008; a $0.02 budget funds only a couple.
    result = asyncio.run(
        run_market(_FIELD, shadow=False, budget_usd=0.02, conn=tmp_db)
    )
    assert result.spent_usd <= 0.02 + 1e-9
