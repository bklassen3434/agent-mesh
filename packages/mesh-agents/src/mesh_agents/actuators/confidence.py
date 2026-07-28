"""Confidence actuator — auto-tune the evidence→confidence weights (config kind).

The first non-prompt, non-LLM actuator. A ``confidence`` concern means the KB is sure
about beliefs the web contradicts — a *calibration* fault. The fix is numeric, and it
shadow-tests **for free**: recompute each already-graded belief's confidence under the
candidate weights and score how well confidence matches the web verdict (supported→1,
contradicted→0). Better calibration on real graded beliefs wins.

Draft nudges the weights toward skepticism (weight the attack term up); shadow
recomputes control vs candidate over the grade ledger (pure Python, no model calls);
promote writes the winning weights to the per-field config the live pipeline reads.
"""
from __future__ import annotations

from typing import Any

from mesh_models.improvement_concern import ImprovementConcern
from mesh_models.improvement_experiment import ExperimentArm, ImprovementExperiment

from mesh_agents.actuators.base import register_actuator

# Min labelled beliefs before a calibration comparison means anything.
_MIN_LABELLED = 8
# How hard one draft nudges the attack term up (capped at 1.0).
_ATTACK_STEP = 0.2


@register_actuator
class ConfidenceActuator:
    component = "confidence"
    target = "confidence-weights"
    uses_llm = False

    def draft(
        self, conn: Any, field_id: str, concerns: list[ImprovementConcern]
    ) -> dict[str, Any] | None:
        from mesh_agents.confidence import ConfidenceWeights

        w = ConfidenceWeights.resolve(conn, field_id)
        candidate = min(1.0, w.attack_weight + _ATTACK_STEP)
        if candidate <= w.attack_weight:
            return None  # already maxed — nothing to try
        # Only fire if there are enough labelled beliefs to judge the change.
        if len(self._labelled(conn, field_id)) < _MIN_LABELLED:
            return None
        return {"weights": {"confidence.attack_weight": candidate}}

    def shadow_sample(
        self, conn: Any, field_id: str, exp: ImprovementExperiment
    ) -> list[tuple[ExperimentArm, float]]:
        from mesh_db.beliefs import get_belief_signals

        from mesh_agents.confidence import (
            BeliefSignals,
            ConfidenceWeights,
            compute_confidence,
        )

        labelled = self._labelled(conn, field_id)
        if len(labelled) < _MIN_LABELLED:
            return []
        control_w = ConfidenceWeights.resolve(conn, field_id)
        treatment_w = control_w.overlay(exp.treatment.get("weights", {}))

        out: list[tuple[ExperimentArm, float]] = []
        for belief_id, verdict_weight in labelled:
            signals = BeliefSignals.from_row(get_belief_signals(conn, belief_id))
            for arm, weights in (
                (ExperimentArm.control, control_w),
                (ExperimentArm.treatment, treatment_w),
            ):
                conf = compute_confidence(signals, weights)
                # calibration: 1 when the confidence matches the web verdict.
                out.append((arm, 1.0 - abs(conf - verdict_weight)))
        return out

    def promote_effects(self, exp: ImprovementExperiment) -> list[Any]:
        from mesh_models.effect import SetFieldConfigEffect

        weights = {k: float(v) for k, v in exp.treatment.get("weights", {}).items()}
        c, t = exp.control_score or 0.0, exp.treatment_score or 0.0
        return [
            SetFieldConfigEffect(
                field_id=exp.field_id, values=weights,
                rationale=f"shadow A/B promoted — calibration {c:.3f} → {t:.3f}",
            )
        ]

    @staticmethod
    def _labelled(conn: Any, field_id: str) -> list[tuple[str, float]]:
        try:
            from mesh_db.belief_grades import recent_grades

            return recent_grades(conn, field_id, limit=200)
        except Exception:
            return []
