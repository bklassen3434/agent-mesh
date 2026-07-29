"""Config-kind actuators: the field_config store + the confidence/decay/resolution
actuators, plus the challenge (prompt-kind) actuator."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


# --------------------------------------------------------------------------
# the entity-resolution actuator (config kind — thresholds; LLM-oracle shadow)
# --------------------------------------------------------------------------


def test_resolution_resolve_overlays_the_store_on_env(tmp_db: Any) -> None:
    from mesh_agents.entity_resolution import ResolutionConfig
    from mesh_db.field_config import set_field_config

    fid = _reset(tmp_db)
    assert ResolutionConfig.resolve(tmp_db, fid).high == 0.93  # env default
    set_field_config(tmp_db, fid, {"entity_resolution.high": 0.97})
    assert ResolutionConfig.resolve(tmp_db, fid).high == 0.97  # overlaid
    _reset(tmp_db)


def test_resolution_draft_nudges_high_up(monkeypatch: pytest.MonkeyPatch) -> None:
    from mesh_agents.actuators.entity_resolution import EntityResolutionActuator

    act = EntityResolutionActuator()
    # enough real candidate pairs → draft a stricter (higher) auto-merge band
    monkeypatch.setattr(act, "_candidate_pairs", lambda *a, **k: [("a", "b", 0.94)] * 5)
    concerns: list[Any] = [SimpleNamespace(severity=0.8)]
    treatment = act.draft(None, "fld", concerns)
    assert treatment == {"config": {"entity_resolution.high": 0.95}}  # 0.93 + 0.02


def test_resolution_draft_declines_without_enough_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mesh_agents.actuators.entity_resolution import EntityResolutionActuator

    act = EntityResolutionActuator()
    monkeypatch.setattr(act, "_candidate_pairs", lambda *a, **k: [("a", "b", 0.94)] * 2)
    few: list[Any] = [SimpleNamespace(severity=0.8)]
    assert act.draft(None, "fld", few) is None


def test_resolution_shadow_scores_band_agreement_per_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mesh_agents.actuators.entity_resolution import EntityResolutionActuator
    from mesh_models.improvement_experiment import ExperimentArm, ImprovementExperiment

    act = EntityResolutionActuator()
    # A pair sitting between control.high (0.93) and treatment.high (0.95): control
    # auto-MERGES it, treatment DEFERS it to the oracle.
    monkeypatch.setattr(act, "_candidate_pairs", lambda *a, **k: [("a", "b", 0.94)] * 6)
    monkeypatch.setattr(act, "_llm", lambda: object())
    # oracle says these are DIFFERENT entities → the control auto-merge is a false merge.
    monkeypatch.setattr(act, "_oracle_same", lambda *a, **k: False)
    exp = ImprovementExperiment(
        field_id="fld", component="entity_resolution", target="merge-candidate",
        treatment={"config": {"entity_resolution.high": 0.95}},
    )
    samples = act.shadow_sample(None, "fld", exp)
    assert len(samples) == 12  # 6 pairs, 2 arms
    control = [s for a, s in samples if a is ExperimentArm.control]
    treatment = [s for a, s in samples if a is ExperimentArm.treatment]
    # control auto-merges (wrong → 0.0); treatment defers to the oracle (correct → 1.0)
    assert sum(control) == 0.0
    assert sum(treatment) == 6.0


def test_resolution_promote_writes_field_config() -> None:
    from mesh_agents.actuators.entity_resolution import EntityResolutionActuator
    from mesh_models.effect import SetFieldConfigEffect
    from mesh_models.improvement_experiment import ImprovementExperiment

    exp = ImprovementExperiment(
        field_id="fld", component="entity_resolution", target="merge-candidate",
        treatment={"config": {"entity_resolution.high": 0.95}},
        control_n=6, control_score_sum=3.0, treatment_n=6, treatment_score_sum=6.0,
    )
    effects = EntityResolutionActuator().promote_effects(exp)
    assert [type(e) for e in effects] == [SetFieldConfigEffect]
    assert effects[0].values == {"entity_resolution.high": 0.95}
    assert effects[0].field_id == "fld"


# --------------------------------------------------------------------------
# the decay actuator (config kind — half-life; free calibration shadow, no LLM)
# --------------------------------------------------------------------------


def test_decay_resolve_overlays_the_store_on_env(tmp_db: Any) -> None:
    from mesh_agents.belief_reconcile import DecayConfig
    from mesh_db.field_config import set_field_config

    fid = _reset(tmp_db)
    assert DecayConfig.resolve(tmp_db, fid).halflife_days == 90.0  # env default
    set_field_config(tmp_db, fid, {"decay.halflife_days": 45.0})
    assert DecayConfig.resolve(tmp_db, fid).halflife_days == 45.0  # overlaid
    _reset(tmp_db)


def test_decay_draft_shortens_halflife(monkeypatch: pytest.MonkeyPatch) -> None:
    from mesh_agents.actuators.decay import DecayActuator

    act = DecayActuator()
    monkeypatch.setattr(act, "_labelled", lambda conn, fid: [("b", 0.0)] * 10)
    concerns: list[Any] = [SimpleNamespace(severity=0.6)]
    treatment = act.draft(None, "fld", concerns)
    assert treatment == {"config": {"decay.halflife_days": 67.5}}  # 90 * 0.75


def test_decay_draft_declines_without_enough_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mesh_agents.actuators.decay import DecayActuator

    act = DecayActuator()
    monkeypatch.setattr(act, "_labelled", lambda conn, fid: [("b", 0.0)] * 3)
    few: list[Any] = [SimpleNamespace(severity=0.6)]
    assert act.draft(None, "fld", few) is None


def test_decay_shadow_scores_calibration_per_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mesh_db.beliefs as beliefs_mod
    from mesh_agents.actuators.decay import DecayActuator
    from mesh_models.improvement_experiment import ExperimentArm, ImprovementExperiment

    act = DecayActuator()
    # 8 contradicted beliefs (weight 0.0) — we want their confidence decayed toward 0.
    monkeypatch.setattr(act, "_labelled", lambda conn, fid: [(f"b{i}", 0.0) for i in range(8)])
    # each belief is old (200 days since revision) and currently held at high confidence
    old = datetime.now(UTC) - timedelta(days=200)
    monkeypatch.setattr(
        beliefs_mod, "get_belief_by_id",
        lambda conn, bid: SimpleNamespace(confidence=0.9, last_revised_at=old),
    )
    exp = ImprovementExperiment(
        field_id="fld", component="decay", target="decay-halflife",
        treatment={"config": {"decay.halflife_days": 45.0}},  # faster than 90
    )
    samples = act.shadow_sample(None, "fld", exp)
    assert len(samples) == 16  # 8 beliefs, 2 arms
    control = [s for a, s in samples if a is ExperimentArm.control]
    treatment = [s for a, s in samples if a is ExperimentArm.treatment]
    # faster decay drops these (contradicted) beliefs' confidence closer to 0 → better
    assert sum(treatment) > sum(control)


def test_decay_promote_writes_field_config() -> None:
    from mesh_agents.actuators.decay import DecayActuator
    from mesh_models.effect import SetFieldConfigEffect
    from mesh_models.improvement_experiment import ImprovementExperiment

    exp = ImprovementExperiment(
        field_id="fld", component="decay", target="decay-halflife",
        treatment={"config": {"decay.halflife_days": 45.0}},
        control_n=8, control_score_sum=4.0, treatment_n=8, treatment_score_sum=6.0,
    )
    effects = DecayActuator().promote_effects(exp)
    assert [type(e) for e in effects] == [SetFieldConfigEffect]
    assert effects[0].values == {"decay.halflife_days": 45.0}


# --------------------------------------------------------------------------
# the challenge actuator (prompt kind — skeptic prompt; graded-ledger shadow)
# --------------------------------------------------------------------------


def test_challenge_draft_proposes_a_new_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    from mesh_agents.actuators.challenge import ChallengeActuator

    act = ChallengeActuator()
    monkeypatch.setattr(act, "_labelled", lambda conn, fid: [("b", 0.0)] * 10)
    monkeypatch.setattr(act, "_current_prompt", lambda conn, fid: "OLD SKEPTIC PROMPT")
    monkeypatch.setattr(act, "_propose", lambda cur, concerns: "SHARPER SKEPTIC PROMPT")
    concerns: list[Any] = [
        SimpleNamespace(severity=0.9, verdict="contradicted", summary="missed")
    ]
    treatment = act.draft(None, "fld", concerns)
    assert treatment == {"prompt": "SHARPER SKEPTIC PROMPT"}


def test_challenge_draft_declines_on_no_change(monkeypatch: pytest.MonkeyPatch) -> None:
    from mesh_agents.actuators.challenge import ChallengeActuator

    act = ChallengeActuator()
    monkeypatch.setattr(act, "_labelled", lambda conn, fid: [("b", 0.0)] * 10)
    monkeypatch.setattr(act, "_current_prompt", lambda conn, fid: "SAME")
    monkeypatch.setattr(act, "_propose", lambda cur, concerns: "SAME")
    concerns: list[Any] = [SimpleNamespace(severity=0.9, verdict="x", summary="y")]
    assert act.draft(None, "fld", concerns) is None


def test_challenge_shadow_scores_web_agreement_per_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mesh_agents.actuators.challenge import ChallengeActuator
    from mesh_models.improvement_experiment import ExperimentArm, ImprovementExperiment

    act = ChallengeActuator()
    # 6 wrong beliefs (weight 0.0 → the skeptic SHOULD flag them)
    monkeypatch.setattr(
        act, "_sample_beliefs",
        lambda conn, fid: [(SimpleNamespace(belief=f"b{i}"), 0.0) for i in range(6)],
    )
    monkeypatch.setattr(act, "_current_prompt", lambda conn, fid: "CONTROL")
    monkeypatch.setattr(act, "_llm", lambda: object())
    # control MISSES them (doesn't flag), treatment CATCHES them (flags)
    monkeypatch.setattr(
        act, "_skeptic_flags",
        lambda llm, inp, prompt: prompt != "CONTROL",
    )
    exp = ImprovementExperiment(
        field_id="fld", component="challenge", target="challenge-belief",
        treatment={"prompt": "TREATMENT"},
    )
    samples = act.shadow_sample(None, "fld", exp)
    assert len(samples) == 12  # 6 beliefs, 2 arms
    control = [s for a, s in samples if a is ExperimentArm.control]
    treatment = [s for a, s in samples if a is ExperimentArm.treatment]
    assert sum(control) == 0.0  # missed every wrong belief
    assert sum(treatment) == 6.0  # caught every wrong belief


def test_challenge_promote_installs_prompt_version() -> None:
    from mesh_agents.actuators.challenge import ChallengeActuator
    from mesh_models.effect import InstallPromptVersionEffect
    from mesh_models.improvement_experiment import ImprovementExperiment

    exp = ImprovementExperiment(
        field_id="fld", component="challenge", target="challenge-belief",
        treatment={"prompt": "SHARPER"},
        control_n=6, control_score_sum=3.0, treatment_n=6, treatment_score_sum=6.0,
    )
    effects = ChallengeActuator().promote_effects(exp)
    assert [type(e) for e in effects] == [InstallPromptVersionEffect]
    v = effects[0].version
    assert v.skill_key == "challenge-belief"
    assert v.prompt == "SHARPER"
    assert v.field_id == "fld"
