"""Decay actuator — auto-tune the belief-aging half-life (config kind, no LLM).

A ``decay`` concern means a belief the world moved past is still held at high
confidence — the aging curve is too slow. The fix is numeric (shorten the
half-life so stale beliefs lose confidence faster) and, like ``confidence``, it
shadow-tests **for free** over the belief-grade ledger: recompute each already-
graded belief's *decayed* confidence (given its age) under the candidate half-life
and score how well that tracks the web verdict (supported→1, contradicted→0).

Faster decay only lowers confidence on beliefs already past the half-life, so it
helps calibration exactly when those stale beliefs are the web-wrong ones, and
*hurts* it if the stale beliefs are still true — so an over-aggressive half-life
loses the A/B and is never promoted. Promotion writes the winning half-life to the
per-field config ``plan_decay_and_archive`` (via ``DecayConfig.resolve``) overlays.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mesh_models.improvement_concern import ImprovementConcern
from mesh_models.improvement_experiment import ExperimentArm, ImprovementExperiment

from mesh_agents.actuators.base import register_actuator

# Min labelled beliefs before a calibration comparison means anything.
_MIN_LABELLED = 8
# How hard one draft shortens the half-life (multiplier), floored so it stays sane.
_HALFLIFE_FACTOR = 0.75
_HALFLIFE_MIN_DAYS = 7.0


@register_actuator
class DecayActuator:
    component = "decay"
    target = "decay-halflife"
    uses_llm = False

    def draft(
        self, conn: Any, field_id: str, concerns: list[ImprovementConcern]
    ) -> dict[str, Any] | None:
        from mesh_agents.belief_reconcile import DecayConfig

        cfg = DecayConfig.resolve(conn, field_id)
        candidate = max(_HALFLIFE_MIN_DAYS, round(cfg.halflife_days * _HALFLIFE_FACTOR, 2))
        if candidate >= cfg.halflife_days:
            return None  # already at the floor — nothing to try
        if len(self._labelled(conn, field_id)) < _MIN_LABELLED:
            return None
        return {"config": {"decay.halflife_days": candidate}}

    def shadow_sample(
        self, conn: Any, field_id: str, exp: ImprovementExperiment
    ) -> list[tuple[ExperimentArm, float]]:
        from mesh_db.beliefs import get_belief_by_id

        from mesh_agents.belief_reconcile import DecayConfig, decayed_confidence

        labelled = self._labelled(conn, field_id)
        if len(labelled) < _MIN_LABELLED:
            return []
        control = DecayConfig.resolve(conn, field_id)
        treatment = control.overlay(exp.treatment.get("config", {}))
        now = datetime.now(UTC)

        out: list[tuple[ExperimentArm, float]] = []
        for belief_id, verdict_weight in labelled:
            belief = get_belief_by_id(conn, belief_id)
            if belief is None:
                continue
            age_days = (now - belief.last_revised_at).total_seconds() / 86400.0
            for arm, cfg in (
                (ExperimentArm.control, control),
                (ExperimentArm.treatment, treatment),
            ):
                conf = decayed_confidence(belief.confidence, age_days, cfg)
                # calibration: 1 when the decayed confidence matches the web verdict.
                out.append((arm, 1.0 - abs(conf - verdict_weight)))
        return out

    def promote_effects(self, exp: ImprovementExperiment) -> list[Any]:
        from mesh_models.effect import SetFieldConfigEffect

        config = {k: float(v) for k, v in exp.treatment.get("config", {}).items()}
        c, t = exp.control_score or 0.0, exp.treatment_score or 0.0
        return [
            SetFieldConfigEffect(
                field_id=exp.field_id, values=config,
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
