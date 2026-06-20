"""The deterministic rule engine (pure — no DB, no LLM).

Builds a :class:`ControllerState` by hand (tensions + stored counters + a fixed
``now``) and asserts what ``plan()`` decides. This is where the auction-free
behaviour is pinned down: explicit priority ordering, escalation-to-swarm on
stalled tensions, and the temporal-as-state scout rule.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from mesh_agents.rules import (
    P_ESCALATE,
    ControllerState,
    plan,
)
from mesh_db.controller_state import DispatchOutcome, TensionState
from mesh_models.tension import Tension, TensionKind

_FIELD = "field-1"
_NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)

# kind → the handler skill the agenda assigns (mirrors agenda._KIND_SKILL).
_HANDLER = {
    TensionKind.unscouted_connector: "scout-source",
    TensionKind.unextracted_source: "extract-source",
    TensionKind.merge_candidate: "merge-candidate",
    TensionKind.redundant_beliefs: "consolidate-beliefs",
    TensionKind.unsynthesized_claims: "synthesize-belief",
    TensionKind.contested_claim: "challenge-belief",
    TensionKind.under_evidenced_entity: "investigate-gap",
}


def _tension(kind: TensionKind, target: str, value: float = 0.5) -> Tension:
    return Tension(
        id=f"{kind.value}:{target}",
        field_id=_FIELD,
        kind=kind,
        subject=target,
        rationale="test",
        value=value,
        est_cost_usd=0.01,
        handler_skill=_HANDLER[kind],
    )


def _state(
    tensions: list[Tension],
    states: dict[str, TensionState] | None = None,
    *,
    now: datetime = _NOW,
    dispatched: set[str] | None = None,
) -> ControllerState:
    return ControllerState(
        field_id=_FIELD,
        field_slug="field-1",
        tensions=tensions,
        states=states or {},
        now=now,
        dispatched=dispatched or set(),
    )


def _st(
    tension_id: str,
    *,
    attempts: int,
    outcome: DispatchOutcome | None = None,
    last_attempt_at: datetime | None = None,
) -> TensionState:
    return TensionState(
        field_id=_FIELD,
        tension_id=tension_id,
        attempts=attempts,
        last_outcome=outcome,
        last_attempt_at=last_attempt_at,
    )


def test_priority_orders_extract_before_synthesize_before_investigate() -> None:
    t_extract = _tension(TensionKind.unextracted_source, "s1")
    t_synth = _tension(TensionKind.unsynthesized_claims, "e1")
    t_invest = _tension(TensionKind.under_evidenced_entity, "e2")
    # Deliberately not in priority order on the board.
    acts = plan(_state([t_invest, t_synth, t_extract]))
    assert [a.skill_id for a in acts] == [
        "extract-source",
        "synthesize-belief",
        "investigate-gap",
    ]
    assert all(a.fanout == 1 for a in acts)


def test_salience_breaks_ties_within_a_priority_tier() -> None:
    low = _tension(TensionKind.unextracted_source, "lo", value=0.2)
    high = _tension(TensionKind.unextracted_source, "hi", value=0.9)
    acts = plan(_state([low, high]))
    # Same priority tier → higher value (salience) first.
    assert [a.tension.subject for a in acts] == ["hi", "lo"]


def test_consolidate_redundant_beliefs_routes_to_skill() -> None:
    t = _tension(TensionKind.redundant_beliefs, "b1:b2")
    acts = plan(_state([t]))
    assert len(acts) == 1
    assert acts[0].skill_id == "consolidate-beliefs"


def test_dispatched_tensions_are_excluded() -> None:
    t = _tension(TensionKind.unextracted_source, "s1")
    acts = plan(_state([t], dispatched={t.id}))
    assert acts == []


def test_escalation_preempts_handler_after_stalled_attempts() -> None:
    t = _tension(TensionKind.unsynthesized_claims, "e1")
    # 3 attempts, last one produced nothing → stalled past the default threshold.
    states = {t.id: _st(t.id, attempts=3, outcome=DispatchOutcome.no_effects)}
    acts = plan(_state([t], states))
    assert len(acts) == 1  # de-duped: one activation per tension
    act = acts[0]
    assert act.priority == P_ESCALATE  # escalation pre-empts the normal handler
    assert act.skill_id == "synthesize-belief"  # same skill, run as a swarm
    assert act.fanout >= 2  # MESH_CONTROLLER_SWARM_SIZE default is 3


def test_no_escalation_before_threshold_or_when_last_succeeded() -> None:
    t1 = _tension(TensionKind.unsynthesized_claims, "e1")
    t2 = _tension(TensionKind.unsynthesized_claims, "e2")
    states = {
        # below threshold
        t1.id: _st(t1.id, attempts=1, outcome=DispatchOutcome.no_effects),
        # at threshold but last attempt produced effects (not stalled)
        t2.id: _st(t2.id, attempts=5, outcome=DispatchOutcome.effects),
    }
    acts = plan(_state([t1, t2], states))
    assert all(a.fanout == 1 for a in acts)  # no swarm fired


def test_scout_suppressed_while_actionable_work_remains() -> None:
    scout = _tension(TensionKind.unscouted_connector, "arxiv")
    work = _tension(TensionKind.unextracted_source, "s1")
    acts = plan(_state([scout, work]))
    # The board is not idle, so the scout rule holds off — only the real work runs.
    assert [a.skill_id for a in acts] == ["extract-source"]


def test_scout_fires_when_board_idle_and_never_scouted() -> None:
    scout = _tension(TensionKind.unscouted_connector, "arxiv")
    acts = plan(_state([scout]))
    assert len(acts) == 1
    assert acts[0].skill_id == "scout-source"


def test_scout_respects_cooldown() -> None:
    scout = _tension(TensionKind.unscouted_connector, "arxiv")
    # Scouted 100s ago; default cooldown is 600s → still cooling down.
    recent = _st(scout.id, attempts=1, last_attempt_at=_NOW - timedelta(seconds=100))
    assert plan(_state([scout], {scout.id: recent})) == []

    # Scouted 700s ago → cooldown elapsed, the scout fires again.
    stale = _st(scout.id, attempts=1, last_attempt_at=_NOW - timedelta(seconds=700))
    acts = plan(_state([scout], {scout.id: stale}))
    assert [a.skill_id for a in acts] == ["scout-source"]


def test_plan_is_deterministic() -> None:
    tensions = [
        _tension(TensionKind.under_evidenced_entity, "e2"),
        _tension(TensionKind.unextracted_source, "s1"),
        _tension(TensionKind.merge_candidate, "a:b"),
    ]
    first = [a.tension.id for a in plan(_state(tensions))]
    second = [a.tension.id for a in plan(_state(list(reversed(tensions))))]
    assert first == second  # same board → same plan, regardless of input order


@pytest.mark.parametrize("kind", list(_HANDLER))
def test_every_kind_routes_to_its_handler(kind: TensionKind) -> None:
    t = _tension(kind, "x")
    acts = plan(_state([t]))
    # Scout only fires when idle, which it is here (only one tension) → all route.
    assert len(acts) == 1
    assert acts[0].skill_id == _HANDLER[kind]
