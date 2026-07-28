"""The pluggable actuator layer: registry + the extraction actuator's draft/promote."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


def test_registry_exposes_extraction() -> None:
    from mesh_agents.actuators import actuatable_components, get_actuator

    assert "extraction" in actuatable_components()
    act = get_actuator("extraction")
    assert act is not None and act.target == "extract-source"
    assert get_actuator("nonexistent") is None


def test_extraction_draft_returns_prompt_and_passes_the_gradient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mesh_agents.eval as eval_pkg
    from mesh_agents.actuators.extraction import ExtractionActuator

    captured: dict[str, Any] = {}

    def _fake_run(llm: Any, judge: Any, proposer: Any, dataset: Any,
                  *, extra_guidance: str | None = None, **kw: Any) -> Any:
        captured["guidance"] = extra_guidance
        return SimpleNamespace(promote=True, best_prompt="NEW", reason="won")

    monkeypatch.setattr(eval_pkg, "run_improvement", _fake_run)
    act = ExtractionActuator()
    monkeypatch.setattr(act, "_load_dataset", lambda conn, fid: object())
    monkeypatch.setattr(act, "_clients", lambda: (object(), object(), object()))

    concerns: list[Any] = [
        SimpleNamespace(id="c1", severity=0.9, verdict="contradicted", summary="overreached")
    ]
    treatment = act.draft(None, "fld", concerns)
    assert treatment == {"prompt": "NEW", "pre_filter": "won"}
    assert "overreached" in captured["guidance"]  # concerns became the gradient


def test_extraction_draft_declines_when_prefilter_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mesh_agents.eval as eval_pkg
    from mesh_agents.actuators.extraction import ExtractionActuator

    monkeypatch.setattr(
        eval_pkg, "run_improvement",
        lambda *a, **k: SimpleNamespace(promote=False, reason="no"),
    )
    act = ExtractionActuator()
    monkeypatch.setattr(act, "_load_dataset", lambda conn, fid: object())
    monkeypatch.setattr(act, "_clients", lambda: (object(), object(), object()))
    declined: list[Any] = [SimpleNamespace(severity=0.1, verdict="x", summary="s")]
    assert act.draft(None, "fld", declined) is None


def test_extraction_promote_builds_a_prompt_version() -> None:
    from mesh_agents.actuators.extraction import ExtractionActuator
    from mesh_models.effect import InstallPromptVersionEffect
    from mesh_models.improvement_experiment import ImprovementExperiment

    exp = ImprovementExperiment(
        field_id="fld", component="extraction", target="extract-source",
        treatment={"prompt": "WINNER"},
        control_n=3, control_score_sum=1.5, treatment_n=3, treatment_score_sum=2.4,
    )
    effects = ExtractionActuator().promote_effects(exp)
    assert [type(e) for e in effects] == [InstallPromptVersionEffect]
    v = effects[0].version
    assert v.skill_key == "extract-source" and v.prompt == "WINNER"
    assert v.holdout_gain == pytest.approx(0.3)  # 0.8 - 0.5
