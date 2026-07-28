"""Skill: ``advance-experiment`` — drive one shadow A/B, decide when it's ripe.

Generic over the actuator registry. A ``running_experiment`` tension routes here for
each open shadow experiment. Two modes, chosen by the experiment's state:

- **not ready** — ask the component's actuator to ``shadow_sample``: run BOTH the
  live config (control) and the candidate (treatment) on real inputs and score each.
  The candidate's output is **discarded** — nothing it produces enters the knowledge
  base (that's what makes it a shadow run). Sampling is cooldown-paced by the rule.
- **ready** (both arms have ``min_sample`` samples) — decide: if the candidate beats
  the live arm by the margin, promote it (the actuator's ``promote_effects`` +
  resolve the concerns that opened it); otherwise reject. Either way close it.

So a change goes live only after winning on real inputs over a window.
"""
from __future__ import annotations

import asyncio
from typing import Any

from mesh_models.effect import (
    DecideExperimentEffect,
    RecordExperimentSampleEffect,
    ResolveConcernsEffect,
)
from mesh_models.tension import Tension, TensionKind

from mesh_agents.skill import register_skill


@register_skill
class AdvanceExperimentSkill:
    """Sample both arms of a shadow A/B on real inputs, and decide when ripe."""

    skill_id = "advance-experiment"
    handles = (TensionKind.running_experiment,)

    async def run(self, conn: Any, tension: Tension, *, budget_usd: float) -> list[Any]:
        from mesh_db.improvement_experiments import get_running_experiment

        from mesh_agents.actuators import get_actuator

        field_id = tension.field_id
        component = tension.target_ref.get("component", "")
        target = tension.target_ref.get("target", "")
        exp = get_running_experiment(conn, field_id, component, target)
        actuator = get_actuator(component)
        if exp is None or actuator is None:
            return []

        if exp.ready:
            return self._decide(exp, actuator)

        try:
            samples = await asyncio.to_thread(
                actuator.shadow_sample, conn, field_id, exp
            )
        except Exception:
            return []
        return [
            RecordExperimentSampleEffect(experiment_id=exp.id, arm=arm, score=score)
            for arm, score in samples
        ]

    def _decide(self, exp: Any, actuator: Any) -> list[Any]:
        c, t = exp.control_score, exp.treatment_score
        summary = (
            f"control {c:.3f} (n={exp.control_n}) vs treatment {t:.3f} "
            f"(n={exp.treatment_n})"
        )
        if not exp.treatment_wins:
            return [
                DecideExperimentEffect(
                    experiment_id=exp.id, promoted=False,
                    rationale=f"rejected — {summary}, margin {exp.margin:.3f}",
                )
            ]
        effects: list[Any] = [
            DecideExperimentEffect(
                experiment_id=exp.id, promoted=True, rationale=f"promoted — {summary}",
            )
        ]
        promote = actuator.promote_effects(exp)
        effects.extend(promote)
        # Resolve the concerns this experiment set out to fix, tagged with whatever
        # the actuator installed (the first promote effect carries an id when it's a
        # versioned install; else the experiment id).
        resolved_by = _resolved_by(promote, exp)
        effects.append(
            ResolveConcernsEffect(concern_ids=list(exp.concern_ids), resolved_by=resolved_by)
        )
        return effects


def _resolved_by(promote_effects: list[Any], exp: Any) -> str:
    for e in promote_effects:
        version = getattr(e, "version", None)
        if version is not None and getattr(version, "id", None):
            return str(version.id)
    return str(exp.id)
