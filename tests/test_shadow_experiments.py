"""The shadow experiment engine: candidate tested beside prod, windowed decision."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from mesh_models.field import DEFAULT_FIELD_ID
from mesh_models.improvement_experiment import (
    ExperimentArm,
    ImprovementExperiment,
)
from mesh_models.tension import ReasoningTier, Tension, TensionKind

_NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)
_REF = {"component": "extraction", "target": "extract-source"}


def _exp(**kw: Any) -> ImprovementExperiment:
    return ImprovementExperiment(
        field_id=DEFAULT_FIELD_ID, component="extraction", target="extract-source",
        treatment_prompt="CANDIDATE", concern_ids=["c1"], min_sample=2, margin=0.05, **kw,
    )


# --------------------------------------------------------------------------
# model — the decision math
# --------------------------------------------------------------------------


def test_experiment_decision_props() -> None:
    e = _exp(control_n=2, control_score_sum=1.0, treatment_n=2, treatment_score_sum=1.8)
    assert e.control_score == 0.5 and e.treatment_score == 0.9
    assert e.ready and e.treatment_wins  # 0.9 > 0.5 + 0.05

    tie = _exp(control_n=2, control_score_sum=1.6, treatment_n=2, treatment_score_sum=1.62)
    assert tie.ready and not tie.treatment_wins  # margin not cleared

    thin = _exp(control_n=2, control_score_sum=1.0, treatment_n=1, treatment_score_sum=0.9)
    assert not thin.ready and not thin.treatment_wins  # treatment under min_sample


# --------------------------------------------------------------------------
# store + gateway
# --------------------------------------------------------------------------


def _reset(conn: Any) -> None:
    conn.execute(
        "DELETE FROM improvement_experiments WHERE field_id = %s", [DEFAULT_FIELD_ID]
    )


def test_open_dedupes_running_record_sample_and_decide(tmp_db: Any) -> None:
    from mesh_db.improvement_experiments import (
        decide_experiment,
        get_running_experiment,
        open_experiment,
        record_sample,
    )

    _reset(tmp_db)
    e = _exp()
    assert open_experiment(tmp_db, e) is not None
    assert open_experiment(tmp_db, _exp()) is None  # one already running for (f,comp,target)

    record_sample(tmp_db, e.id, ExperimentArm.control, 0.4)
    record_sample(tmp_db, e.id, ExperimentArm.control, 0.6)
    record_sample(tmp_db, e.id, ExperimentArm.treatment, 0.9)
    live = get_running_experiment(tmp_db, DEFAULT_FIELD_ID, "extraction", "extract-source")
    assert live is not None
    assert live.control_n == 2 and live.control_score == 0.5
    assert live.treatment_n == 1 and live.treatment_score == 0.9

    decide_experiment(tmp_db, e.id, promoted=True, rationale="won")
    assert get_running_experiment(
        tmp_db, DEFAULT_FIELD_ID, "extraction", "extract-source"
    ) is None  # no longer running
    # after a decision a fresh experiment may open again
    assert open_experiment(tmp_db, _exp()) is not None
    _reset(tmp_db)


def test_experiment_effects_through_gateway(tmp_db: Any) -> None:
    from mesh_db.effects import apply_effects
    from mesh_db.improvement_experiments import get_running_experiment
    from mesh_models.effect import (
        DecideExperimentEffect,
        OpenExperimentEffect,
        RecordExperimentSampleEffect,
    )

    _reset(tmp_db)
    e = _exp()
    r1 = apply_effects(tmp_db, [OpenExperimentEffect(experiment=e)])
    assert r1.experiments_opened == 1
    r2 = apply_effects(tmp_db, [
        RecordExperimentSampleEffect(experiment_id=e.id, arm=ExperimentArm.treatment, score=0.8),
    ])
    assert r2.experiment_samples_recorded == 1
    r3 = apply_effects(tmp_db, [
        DecideExperimentEffect(experiment_id=e.id, promoted=False, rationale="lost"),
    ])
    assert r3.experiments_decided == 1
    assert get_running_experiment(
        tmp_db, DEFAULT_FIELD_ID, "extraction", "extract-source"
    ) is None
    _reset(tmp_db)


# --------------------------------------------------------------------------
# producer + rule
# --------------------------------------------------------------------------


def test_producer_emits_a_tension_per_running_experiment(tmp_db: Any) -> None:
    from mesh_agents.agenda import experiment_tensions
    from mesh_db.improvement_experiments import open_experiment

    _reset(tmp_db)
    open_experiment(tmp_db, _exp())
    tensions = experiment_tensions(tmp_db, DEFAULT_FIELD_ID)
    assert len(tensions) == 1
    t = tensions[0]
    assert t.kind is TensionKind.running_experiment
    assert t.handler_skill == "advance-experiment"
    assert t.signals["ready"] is False  # no samples yet
    _reset(tmp_db)


def test_advance_rule_fires_immediately_when_ready_else_cooldown_gated() -> None:
    from datetime import timedelta as _td

    from mesh_agents.rules import (
        ControllerState,
        experiment_sample_cooldown_seconds,
        plan,
    )
    from mesh_db.controller_state import TensionState

    def _t(ready: bool) -> Tension:
        return Tension(
            id="running_experiment:e1", field_id="f", kind=TensionKind.running_experiment,
            subject="exp", rationale="t", value=0.3, est_cost_usd=0.2,
            handler_skill="advance-experiment", tier=ReasoningTier.simple,
            target_ref={"experiment_id": "e1", **_REF},
            signals={"ready": ready},
        )

    def _state(t: Tension, st: TensionState | None = None) -> ControllerState:
        return ControllerState(
            field_id="f", field_slug="f", tensions=[t],
            states={t.id: st} if st else {}, now=_NOW,
        )

    # ready → fires even with a very recent attempt (decision isn't paced)
    recent = TensionState(
        field_id="f", tension_id="running_experiment:e1", last_attempt_at=_NOW,
    )
    assert len(plan(_state(_t(True), recent))) == 1

    # not ready + sampled just now → held by the cooldown
    assert plan(_state(_t(False), recent)) == []

    # not ready + cooldown elapsed → fires to gather more
    old = TensionState(
        field_id="f", tension_id="running_experiment:e1",
        last_attempt_at=_NOW - _td(seconds=experiment_sample_cooldown_seconds() + 10),
    )
    assert len(plan(_state(_t(False), old))) == 1


# --------------------------------------------------------------------------
# advance-experiment skill — the decision
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advance_decides_promote_installs_and_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mesh_db.improvement_experiments as exp_mod
    from mesh_agents.skills.advance_experiment import AdvanceExperimentSkill
    from mesh_models.effect import (
        DecideExperimentEffect,
        InstallPromptVersionEffect,
        ResolveConcernsEffect,
    )

    won = _exp(control_n=2, control_score_sum=1.0, treatment_n=2, treatment_score_sum=1.9)
    monkeypatch.setattr(exp_mod, "get_running_experiment", lambda *a, **k: won)

    t = Tension(
        id=f"running_experiment:{won.id}", field_id=DEFAULT_FIELD_ID,
        kind=TensionKind.running_experiment, subject="exp", rationale="t",
        value=0.35, est_cost_usd=0.2, handler_skill="advance-experiment",
        target_ref={"experiment_id": won.id, "component": "extraction", "target": "extract-source"},
        signals={"ready": True},
    )
    effects = await AdvanceExperimentSkill().run(None, t, budget_usd=1.0)
    assert [type(e) for e in effects] == [
        DecideExperimentEffect, InstallPromptVersionEffect, ResolveConcernsEffect
    ]
    decide, install, resolve = effects
    assert decide.promoted is True
    assert install.version.prompt == "CANDIDATE"
    assert resolve.concern_ids == ["c1"]
    assert resolve.resolved_by == install.version.id


@pytest.mark.asyncio
async def test_advance_decides_reject_when_treatment_does_not_win(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mesh_db.improvement_experiments as exp_mod
    from mesh_agents.skills.advance_experiment import AdvanceExperimentSkill
    from mesh_models.effect import DecideExperimentEffect

    tie = _exp(control_n=2, control_score_sum=1.6, treatment_n=2, treatment_score_sum=1.62)
    monkeypatch.setattr(exp_mod, "get_running_experiment", lambda *a, **k: tie)
    t = Tension(
        id=f"running_experiment:{tie.id}", field_id=DEFAULT_FIELD_ID,
        kind=TensionKind.running_experiment, subject="exp", rationale="t",
        value=0.35, est_cost_usd=0.2, handler_skill="advance-experiment",
        target_ref={"experiment_id": tie.id, "component": "extraction", "target": "extract-source"},
        signals={"ready": True},
    )
    effects = await AdvanceExperimentSkill().run(None, t, budget_usd=1.0)
    assert [type(e) for e in effects] == [DecideExperimentEffect]
    assert effects[0].promoted is False  # rejected — nothing installed


@pytest.mark.asyncio
async def test_advance_samples_both_arms_when_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mesh_db.improvement_experiments as exp_mod
    from mesh_agents.eval.extraction import ExtractionExample
    from mesh_agents.skills.advance_experiment import AdvanceExperimentSkill
    from mesh_models.effect import RecordExperimentSampleEffect

    running = _exp(control_n=0, treatment_n=0)  # not ready
    monkeypatch.setattr(exp_mod, "get_running_experiment", lambda *a, **k: running)

    # one stub judge/llm; sampler returns one real-ish source example
    class _LLM:
        def complete_with_usage(self, *a: Any, **k: Any) -> Any:
            from mesh_agents.claim_extractor import ClaimExtractionResult
            return ClaimExtractionResult(claims=[]), 1, object()

    class _Judge:
        def judge(self, *a: Any, **k: Any) -> Any:
            from mesh_agents.eval.extraction import ExtractionVerdict
            return ExtractionVerdict(coverage=0.0)

    skill = AdvanceExperimentSkill(llm=_LLM(), judge=_Judge())
    monkeypatch.setattr(
        skill, "_sample_sources",
        lambda conn, fid: [ExtractionExample(id="s1", source_type="blog", title="T", abstract="A")],
    )
    # avoid a live DB read for the control prompt
    monkeypatch.setattr(
        "mesh_agents.claim_extractor.resolve_extraction_system",
        lambda *a, **k: "LIVE PROMPT",
    )

    t = Tension(
        id=f"running_experiment:{running.id}", field_id=DEFAULT_FIELD_ID,
        kind=TensionKind.running_experiment, subject="exp", rationale="t",
        value=0.2, est_cost_usd=0.2, handler_skill="advance-experiment",
        target_ref={"experiment_id": running.id, **_REF},
        signals={"ready": False},
    )
    effects = await skill.run(None, t, budget_usd=1.0)
    # one source → one control sample + one treatment sample
    arms = sorted(e.arm.value for e in effects if isinstance(e, RecordExperimentSampleEffect))
    assert arms == ["control", "treatment"]
