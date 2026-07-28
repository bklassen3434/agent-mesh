"""The self-improvement loop wired onto the whiteboard.

- **producer (DB)** — improvement_tensions derives an improvable_component tension
  only once open concerns cross the threshold, and only for a component with a
  wired actuator.
- **rules (pure)** — plan() fires sense-accuracy on the eval cooldown and
  improve-component off the concern-threshold tension, backing off under the stall
  cooldown.
- **skill (stubs)** — improve-component promotes (Install + Resolve effects) iff the
  A/B says so, and no-ops for a component without an actuator.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from mesh_agents.rules import (
    P_EVALUATE,
    P_IMPROVE,
    ControllerState,
    evaluate_cooldown_seconds,
    plan,
    stall_cooldown_seconds,
)
from mesh_db.controller_state import DispatchOutcome, TensionState
from mesh_models.improvement_concern import ConcernComponent, ImprovementConcern
from mesh_models.tension import ReasoningTier, Tension, TensionKind

_NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# producer (DB): concerns cross a threshold → a tension
# --------------------------------------------------------------------------


def _reset(conn: Any) -> str:
    from mesh_models.field import DEFAULT_FIELD_ID

    conn.execute("DELETE FROM improvement_concerns WHERE field_id = %s", [DEFAULT_FIELD_ID])
    return DEFAULT_FIELD_ID


def _file(conn: Any, field_id: str, component: ConcernComponent, bid: str,
          target: str, severity: float = 0.3) -> None:
    from mesh_db.improvement_concerns import record_concern

    record_concern(
        conn,
        ImprovementConcern(
            field_id=field_id, component=component, target=target,
            belief_id=bid, verdict="contradicted", severity=severity, summary="s",
        ),
    )


def test_producer_fires_only_when_extraction_concerns_cross_threshold(
    tmp_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mesh_agents.agenda import improvement_tensions

    monkeypatch.setenv("MESH_IMPROVE_CONCERN_THRESHOLD", "3")
    monkeypatch.setenv("MESH_IMPROVE_SEVERITY_THRESHOLD", "99")  # count-driven for this test
    field_id = _reset(tmp_db)

    _file(tmp_db, field_id, ConcernComponent.extraction, "b1", "extract-source")
    _file(tmp_db, field_id, ConcernComponent.extraction, "b2", "extract-source")
    assert improvement_tensions(tmp_db, field_id) == []  # 2 < threshold

    _file(tmp_db, field_id, ConcernComponent.extraction, "b3", "extract-source")
    tensions = improvement_tensions(tmp_db, field_id)
    assert len(tensions) == 1
    t = tensions[0]
    assert t.kind is TensionKind.improvable_component
    assert t.target_ref == {"component": "extraction", "target": "extract-source"}
    assert t.handler_skill == "improve-component"
    _reset(tmp_db)


def test_producer_ignores_components_without_a_wired_actuator(
    tmp_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mesh_agents.agenda import improvement_tensions

    monkeypatch.setenv("MESH_IMPROVE_CONCERN_THRESHOLD", "1")
    field_id = _reset(tmp_db)
    # plenty of synthesis/freshness concerns, but no actuator → no tension yet
    _file(tmp_db, field_id, ConcernComponent.synthesis, "b1", "synthesize-belief")
    _file(tmp_db, field_id, ConcernComponent.freshness, "b2", "")
    assert improvement_tensions(tmp_db, field_id) == []
    _reset(tmp_db)


# --------------------------------------------------------------------------
# rules (pure): cooldowns via plan()
# --------------------------------------------------------------------------


def _tension(kind: TensionKind, target: str, skill: str) -> Tension:
    return Tension(
        id=f"{kind.value}:{target}", field_id="f", kind=kind, subject=target,
        rationale="t", value=0.3, est_cost_usd=0.5, handler_skill=skill,
        tier=ReasoningTier.simple, target_ref={"component": "extraction", "target": target},
    )


def _state(tensions: list[Tension], states: dict[str, TensionState] | None = None,
           now: datetime = _NOW) -> ControllerState:
    return ControllerState(
        field_id="f", field_slug="f", tensions=tensions, states=states or {}, now=now,
    )


def test_evaluate_accuracy_fires_when_never_graded_then_respects_cooldown() -> None:
    t = _tension(TensionKind.evaluate_accuracy, "f", "sense-accuracy")

    acts = plan(_state([t]))  # never graded → fires
    assert [(a.skill_id, a.priority) for a in acts] == [("sense-accuracy", P_EVALUATE)]

    recent = TensionState(
        field_id="f",
        tension_id=t.id,
        last_attempt_at=_NOW - timedelta(seconds=evaluate_cooldown_seconds() - 100),
    )
    assert plan(_state([t], {t.id: recent})) == []  # graded too recently → held

    old = TensionState(
        field_id="f",
        tension_id=t.id,
        last_attempt_at=_NOW - timedelta(seconds=evaluate_cooldown_seconds() + 100),
    )
    assert len(plan(_state([t], {t.id: old}))) == 1  # cooldown elapsed → fires again


def test_improve_component_fires_then_backs_off_under_stall_cooldown() -> None:
    t = _tension(TensionKind.improvable_component, "extract-source", "improve-component")

    acts = plan(_state([t]))  # concerns already crossed threshold (tension exists) → fire
    assert [(a.skill_id, a.priority) for a in acts] == [("improve-component", P_IMPROVE)]

    # a just-stalled A/B (produced no effects) backs off for the stall cooldown
    stalled = TensionState(
        field_id="f",
        tension_id=t.id,
        attempts=1,
        last_outcome=DispatchOutcome.no_effects,
        last_attempt_at=_NOW - timedelta(seconds=stall_cooldown_seconds() - 50),
    )
    assert plan(_state([t], {t.id: stalled})) == []

    # once the cooldown elapses it re-fires
    cooled = TensionState(
        field_id="f",
        tension_id=t.id,
        attempts=1,
        last_outcome=DispatchOutcome.no_effects,
        last_attempt_at=_NOW - timedelta(seconds=stall_cooldown_seconds() + 50),
    )
    assert len(plan(_state([t], {t.id: cooled}))) == 1


# --------------------------------------------------------------------------
# skill (stubs): promote iff the A/B wins; no-op without an actuator
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_improve_component_skill_no_op_without_actuator() -> None:
    from mesh_agents.skills.improve_component import ImproveComponentSkill

    t = Tension(
        id="improvable_component:synthesis", field_id="f",
        kind=TensionKind.improvable_component, subject="synthesis", rationale="t",
        value=0.3, est_cost_usd=0.5, handler_skill="improve-component",
        target_ref={"component": "synthesis", "target": "synthesize-belief"},
    )
    effects = await ImproveComponentSkill().run(None, t, budget_usd=1.0)
    assert effects == []  # synthesis has no wired A/B actuator → nothing


@pytest.mark.asyncio
async def test_improve_component_skill_promotes_and_resolves_when_ab_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mesh_agents.eval as eval_pkg
    import mesh_db.improvement_concerns as concerns_mod
    from mesh_agents.skills.improve_component import ImproveComponentSkill
    from mesh_models.effect import InstallPromptVersionEffect, ResolveConcernsEffect

    concerns = [
        SimpleNamespace(id="c1", severity=0.9, verdict="contradicted", summary="overreached"),
        SimpleNamespace(id="c2", severity=0.4, verdict="partially_supported", summary="thin"),
    ]
    monkeypatch.setattr(concerns_mod, "list_open_concerns", lambda *a, **k: concerns)

    fake_run = SimpleNamespace(
        promote=True, best_prompt="NEW EXTRACTION PROMPT", dataset_field="fld",
        holdout_baseline_f1=0.3, holdout_best_f1=0.6, holdout_gain=0.3,
        reason="held-out F1 0.30 → 0.60", proposer_tokens=42,
        optimization=SimpleNamespace(baseline_f1=0.3, best_f1=0.65),
    )
    captured: dict[str, Any] = {}

    def _fake_run_improvement(
        llm: Any, judge: Any, proposer: Any, dataset: Any,
        *, extra_guidance: str | None = None, **kw: Any,
    ) -> Any:
        captured["guidance"] = extra_guidance
        return fake_run

    monkeypatch.setattr(eval_pkg, "run_improvement", _fake_run_improvement)

    skill = ImproveComponentSkill(llm=object(), judge=object(), proposer=object())
    monkeypatch.setattr(skill, "_load_dataset", lambda conn, fid: object())

    t = Tension(
        id="improvable_component:extraction", field_id="fld",
        kind=TensionKind.improvable_component, subject="extraction", rationale="t",
        value=0.5, est_cost_usd=0.5, handler_skill="improve-component",
        target_ref={"component": "extraction", "target": "extract-source"},
    )
    effects = await skill.run(None, t, budget_usd=1.0)

    kinds = [type(e) for e in effects]
    assert kinds == [InstallPromptVersionEffect, ResolveConcernsEffect]
    install, resolve = effects
    assert install.version.skill_key == "extract-source"
    assert install.version.prompt == "NEW EXTRACTION PROMPT"
    assert install.version.holdout_gain == 0.3
    assert resolve.concern_ids == ["c1", "c2"]
    assert resolve.resolved_by == install.version.id
    # the accuracy gradient was passed down to the optimizer
    assert "overreached" in captured["guidance"]


@pytest.mark.asyncio
async def test_improve_component_skill_no_effects_when_ab_loses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mesh_agents.eval as eval_pkg
    import mesh_db.improvement_concerns as concerns_mod
    from mesh_agents.skills.improve_component import ImproveComponentSkill

    one = [SimpleNamespace(id="c1", severity=0.5, verdict="contradicted", summary="x")]
    monkeypatch.setattr(concerns_mod, "list_open_concerns", lambda *a, **k: one)
    monkeypatch.setattr(
        eval_pkg, "run_improvement",
        lambda *a, **k: SimpleNamespace(promote=False, reason="did not generalize"),
    )
    skill = ImproveComponentSkill(llm=object(), judge=object(), proposer=object())
    monkeypatch.setattr(skill, "_load_dataset", lambda conn, fid: object())

    t = Tension(
        id="improvable_component:extraction", field_id="fld",
        kind=TensionKind.improvable_component, subject="extraction", rationale="t",
        value=0.5, est_cost_usd=0.5, handler_skill="improve-component",
        target_ref={"component": "extraction", "target": "extract-source"},
    )
    assert await skill.run(None, t, budget_usd=1.0) == []  # no promote → no effects
