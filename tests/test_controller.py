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


# ── swarm reconcile: union (default) vs quorum ───────────────────────────────


def _swarm_activation(batches: list[list[Any]]) -> Any:
    """An Activation whose skill returns one pre-seeded batch per instance (so K
    copies can disagree), with fanout = len(batches)."""
    from mesh_agents.rules import Activation
    from mesh_models.tension import ReasoningTier, Tension, TensionKind

    queue = list(batches)

    @register_skill
    class _Disagree:
        skill_id = "challenge-belief"
        handles = (TensionKind.contested_claim,)

        async def run(self, conn: Any, tension: Any, *, budget_usd: float) -> list[Any]:
            return queue.pop()  # atomic in single-thread asyncio after the semaphore

    tension = Tension(
        id="contested_claim:b1",
        field_id=_FIELD,
        kind=TensionKind.contested_claim,
        subject="b1",
        rationale="x",
        value=0.7,
        est_cost_usd=0.04,
        handler_skill="challenge-belief",
        tier=ReasoningTier.swarm,
    )
    return Activation(
        tension=tension, skill_id="challenge-belief", priority=40, fanout=len(batches), reason="x"
    )


def _belief_effect(topic: str) -> Any:
    return CreateBeliefEffect(
        field_id=_FIELD,
        belief=Belief(topic=topic, statement="x", confidence=0.5, is_currently_held=True),
    )


def test_swarm_union_keeps_every_distinct_effect(monkeypatch: Any, tmp_db: MeshConnection) -> None:
    from mesh_pipeline.controller import _dispatch_one

    monkeypatch.delenv("MESH_CONTROLLER_SWARM_QUORUM", raising=False)  # default: union
    a, b, c = _belief_effect("A"), _belief_effect("B"), _belief_effect("C")
    act = _swarm_activation([[a, b], [a], [a, c]])
    effects, _outcome, _usage, _latency, _err = asyncio.run(
        _dispatch_one(tmp_db, act, asyncio.Semaphore(3))
    )
    topics = sorted(e.belief.topic for e in effects)
    assert topics == ["A", "B", "C"]  # union of all instances


def test_swarm_quorum_keeps_only_majority_effects(
    monkeypatch: Any, tmp_db: MeshConnection
) -> None:
    from mesh_pipeline.controller import _dispatch_one

    monkeypatch.setenv("MESH_CONTROLLER_SWARM_QUORUM", "true")
    a, b, c = _belief_effect("A"), _belief_effect("B"), _belief_effect("C")
    # A in 3/3 instances (≥ ceil(3/2)=2 → kept); B and C in 1/3 each → dropped.
    act = _swarm_activation([[a, b], [a], [a, c]])
    effects, _outcome, _usage, _latency, _err = asyncio.run(
        _dispatch_one(tmp_db, act, asyncio.Semaphore(3))
    )
    assert [e.belief.topic for e in effects] == ["A"]


# ── deep adjudication wired end-to-end through the controller (plan step) ─────


def test_controller_opens_an_adjudication_investigation_for_a_contradiction(
    tmp_db: MeshConnection,
) -> None:
    """One live round on a contradicted load-bearing belief: producer → rule →
    adjudicate-contradiction skill → OpenInvestigationEffect → gateway. The plan
    step needs no LLM, so this proves the deep wiring without mocking the model."""
    from datetime import UTC, datetime

    from mesh_agents.skills.adjudicate_contradiction import AdjudicateContradictionSkill
    from mesh_db.beliefs import create_belief
    from mesh_db.claims import create_claim
    from mesh_db.entities import create_entity
    from mesh_db.investigations import list_investigations
    from mesh_models.claim import Claim
    from mesh_models.entity import Entity, EntityType
    from mesh_models.investigation import InvestigationOrigin

    # The autouse _clean_registry cleared the registry and load_builtin_skills is a
    # no-op once modules are imported (decorators don't re-run), so register the one
    # real skill this round needs directly. The plan step uses no LLM.
    register_skill(AdjudicateContradictionSkill)
    ent = create_entity(tmp_db, Entity(canonical_name="LB", type=EntityType.model))
    src = _unread_source(tmp_db, "adj-src")
    supports = [
        create_claim(
            tmp_db,
            Claim(
                predicate="achieves_score",
                subject_entity_id=ent.id,
                object={"score": 90 - i, "benchmark": "MMLU"},
                source_id=src.id,
                extracted_at=datetime(2026, 6, 13, tzinfo=UTC),
                extracted_by_agent="claim_extractor",
                raw_excerpt="…",
            ),
        ).id
        for i in range(2)
    ]
    against = create_claim(
        tmp_db,
        Claim(
            predicate="critiques",
            subject_entity_id=ent.id,
            object={"note": "fails to reproduce"},
            source_id=src.id,
            extracted_at=datetime(2026, 6, 13, tzinfo=UTC),
            extracted_by_agent="skeptic",
            raw_excerpt="…",
        ),
    )
    create_belief(
        tmp_db,
        Belief(
            topic="lb-sota",
            statement="LB is SOTA",
            supporting_claim_ids=supports,
            contradicting_claim_ids=[against.id],
            confidence=0.85,
            is_currently_held=True,
        ),
    )

    asyncio.run(run_controller(_FIELD, shadow=False, max_rounds=1, conn=tmp_db))

    opened = list_investigations(
        tmp_db, origin=InvestigationOrigin.adjudication, field_id=tmp_db_field_id(tmp_db)
    )
    assert len(opened) == 1
    assert opened[0].opened_by_belief_id is not None


# ── daily LLM budget brake ────────────────────────────────────────────────────


def _seed_usage_today(conn: MeshConnection, *, tokens: int, usd: float) -> None:
    from mesh_db.llm_usage import LLMUsageRecord, create_llm_usage

    create_llm_usage(
        conn,
        LLMUsageRecord(
            run_id="budget-test-run",
            skill_id="extract-source",
            model="claude-haiku-4-5",
            input_tokens=tokens,
            estimated_cost_usd=usd,
        ),
    )


def test_budget_brake_defers_llm_bound_work(
    tmp_db: MeshConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ledger over the token budget → the (uses_llm-presumed) handler is deferred:
    # the plan empties and the controller is quiescent without dispatching.
    monkeypatch.setenv("MESH_DAILY_LLM_BUDGET_TOKENS", "1000")
    monkeypatch.delenv("MESH_DAILY_LLM_BUDGET_USD", raising=False)
    _register_belief_maker()
    _unread_source(tmp_db, "budget-src")
    _seed_usage_today(tmp_db, tokens=2000, usd=0.01)

    result = asyncio.run(run_controller(_FIELD, shadow=False, max_rounds=1, conn=tmp_db))

    assert result.quiescent is True
    assert all(r.dispatched == 0 for r in result.rounds)
    assert list_beliefs(tmp_db, currently_held=True, limit=10) == []


def test_budget_brake_usd_knob(
    tmp_db: MeshConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MESH_DAILY_LLM_BUDGET_TOKENS", raising=False)
    monkeypatch.setenv("MESH_DAILY_LLM_BUDGET_USD", "1.50")
    _register_belief_maker()
    _unread_source(tmp_db, "budget-usd-src")
    _seed_usage_today(tmp_db, tokens=10, usd=2.00)

    result = asyncio.run(run_controller(_FIELD, shadow=False, max_rounds=1, conn=tmp_db))
    assert all(r.dispatched == 0 for r in result.rounds)


def test_budget_brake_lets_llm_free_skills_run(
    tmp_db: MeshConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An over-budget day must NOT stop LLM-free work: a uses_llm=False handler
    # for the same tension still dispatches and its effects apply.
    monkeypatch.setenv("MESH_DAILY_LLM_BUDGET_TOKENS", "1000")

    @register_skill
    class _FreeBeliefMaker:
        skill_id = "extract-source"
        handles = (TensionKind.unextracted_source,)
        uses_llm = False

        async def run(self, conn: Any, tension: Any, *, budget_usd: float) -> list[Any]:
            return [
                CreateBeliefEffect(
                    field_id=tension.field_id,
                    belief=Belief(
                        topic="free-skill",
                        statement="written while budget exhausted",
                        confidence=0.5,
                        is_currently_held=True,
                    ),
                )
            ]

    _unread_source(tmp_db, "budget-free-src")
    _seed_usage_today(tmp_db, tokens=2000, usd=0.01)

    result = asyncio.run(run_controller(_FIELD, shadow=False, max_rounds=1, conn=tmp_db))
    assert any(r.dispatched >= 1 for r in result.rounds)
    assert len(list_beliefs(tmp_db, currently_held=True, limit=10)) == 1


def test_budget_unset_is_a_noop(
    tmp_db: MeshConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MESH_DAILY_LLM_BUDGET_TOKENS", raising=False)
    monkeypatch.delenv("MESH_DAILY_LLM_BUDGET_USD", raising=False)
    _register_belief_maker()
    _unread_source(tmp_db, "budget-off-src")
    _seed_usage_today(tmp_db, tokens=10_000_000, usd=100.0)

    result = asyncio.run(run_controller(_FIELD, shadow=False, max_rounds=1, conn=tmp_db))
    assert any(r.dispatched >= 1 for r in result.rounds)


def test_budget_under_limit_dispatches_normally(
    tmp_db: MeshConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MESH_DAILY_LLM_BUDGET_TOKENS", "1000000")
    monkeypatch.setenv("MESH_DAILY_LLM_BUDGET_USD", "5.0")
    _register_belief_maker()
    _unread_source(tmp_db, "budget-under-src")
    _seed_usage_today(tmp_db, tokens=500, usd=0.01)

    result = asyncio.run(run_controller(_FIELD, shadow=False, max_rounds=1, conn=tmp_db))
    assert any(r.dispatched >= 1 for r in result.rounds)
