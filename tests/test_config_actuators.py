"""Config-kind actuators: the field_config store + the confidence actuator."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from mesh_models.field import DEFAULT_FIELD_ID


def _reset(conn: Any) -> str:
    conn.execute("DELETE FROM field_config WHERE field_id = %s", [DEFAULT_FIELD_ID])
    return DEFAULT_FIELD_ID


# --------------------------------------------------------------------------
# field_config store + gateway + confidence overlay
# --------------------------------------------------------------------------


def test_set_and_get_active_config_is_append_only(tmp_db: Any) -> None:
    from mesh_db.field_config import get_active_config, set_field_config

    fid = _reset(tmp_db)
    set_field_config(tmp_db, fid, {"confidence.attack_weight": 0.7})
    assert get_active_config(tmp_db, fid) == {"confidence.attack_weight": 0.7}
    # a second set for the same key deactivates the first, keeps one active
    set_field_config(tmp_db, fid, {"confidence.attack_weight": 0.9})
    assert get_active_config(tmp_db, fid) == {"confidence.attack_weight": 0.9}
    _reset(tmp_db)


def test_set_field_config_effect_through_gateway(tmp_db: Any) -> None:
    from mesh_db.effects import apply_effects
    from mesh_db.field_config import get_active_config
    from mesh_models.effect import SetFieldConfigEffect

    fid = _reset(tmp_db)
    rep = apply_effects(
        tmp_db,
        [SetFieldConfigEffect(field_id=fid, values={"confidence.attack_weight": 0.8})],
    )
    assert rep.field_config_set == 1
    assert get_active_config(tmp_db, fid) == {"confidence.attack_weight": 0.8}
    _reset(tmp_db)


def test_confidence_resolve_overlays_the_store_on_env(tmp_db: Any) -> None:
    from mesh_agents.confidence import ConfidenceWeights
    from mesh_db.field_config import set_field_config

    fid = _reset(tmp_db)
    assert ConfidenceWeights.resolve(tmp_db, fid).attack_weight == 0.5  # env default
    set_field_config(tmp_db, fid, {"confidence.attack_weight": 0.85})
    assert ConfidenceWeights.resolve(tmp_db, fid).attack_weight == 0.85  # overlaid
    _reset(tmp_db)


# --------------------------------------------------------------------------
# the confidence actuator (pure — no LLM)
# --------------------------------------------------------------------------


def test_confidence_draft_nudges_attack_weight_up(monkeypatch: pytest.MonkeyPatch) -> None:
    from mesh_agents.actuators.confidence import ConfidenceActuator

    act = ConfidenceActuator()
    # plenty of labelled beliefs → draft a stricter candidate
    monkeypatch.setattr(act, "_labelled", lambda conn, fid: [("b", 0.0)] * 10)
    concerns: list[Any] = [SimpleNamespace(severity=0.9)]
    treatment = act.draft(None, "fld", concerns)
    assert treatment == {"weights": {"confidence.attack_weight": 0.7}}  # 0.5 + 0.2


def test_confidence_draft_declines_without_enough_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mesh_agents.actuators.confidence import ConfidenceActuator

    act = ConfidenceActuator()
    monkeypatch.setattr(act, "_labelled", lambda conn, fid: [("b", 0.0)] * 3)  # too few
    few: list[Any] = [SimpleNamespace(severity=0.9)]
    assert act.draft(None, "fld", few) is None


def test_confidence_shadow_scores_calibration_per_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mesh_db.beliefs as beliefs_mod
    from mesh_agents.actuators.confidence import ConfidenceActuator
    from mesh_models.improvement_experiment import ExperimentArm, ImprovementExperiment

    act = ConfidenceActuator()
    # 8 contradicted beliefs (verdict weight 0.0) — we want LOW confidence on them.
    monkeypatch.setattr(act, "_labelled", lambda conn, fid: [(f"b{i}", 0.0) for i in range(8)])
    # signals that yield some support (so higher attack_weight lowers confidence)
    monkeypatch.setattr(
        beliefs_mod, "get_belief_signals",
        lambda conn, bid: {
            "source_type_diversity": 4, "reproduction_count": 3,
            "supporting_claim_count": 8, "skeptic_counter_claim_count": 2,
            "severe_failure_mode_count": 1,
        },
    )
    exp = ImprovementExperiment(
        field_id="fld", component="confidence", target="confidence-weights",
        treatment={"weights": {"confidence.attack_weight": 0.9}},
    )
    samples = act.shadow_sample(None, "fld", exp)
    assert len(samples) == 16  # 8 beliefs, 2 arms
    control = [s for a, s in samples if a is ExperimentArm.control]
    treatment = [s for a, s in samples if a is ExperimentArm.treatment]
    # these beliefs are contradicted (want conf→0); the stricter treatment weighs the
    # attack term more, so it lands closer to 0 → better calibrated (higher score).
    assert sum(treatment) > sum(control)


def test_confidence_promote_writes_field_config() -> None:
    from mesh_agents.actuators.confidence import ConfidenceActuator
    from mesh_models.effect import SetFieldConfigEffect
    from mesh_models.improvement_experiment import ImprovementExperiment

    exp = ImprovementExperiment(
        field_id="fld", component="confidence", target="confidence-weights",
        treatment={"weights": {"confidence.attack_weight": 0.7}},
        control_n=8, control_score_sum=4.0, treatment_n=8, treatment_score_sum=6.0,
    )
    effects = ConfidenceActuator().promote_effects(exp)
    assert [type(e) for e in effects] == [SetFieldConfigEffect]
    assert effects[0].values == {"confidence.attack_weight": 0.7}
    assert effects[0].field_id == "fld"
