"""Skill: ``improve-component`` — draft a fix and OPEN a shadow experiment for it.

Generic over the actuator registry: a derived ``improvable_component`` tension fires
here once a component's accumulated fault-attribution concerns cross the activation
threshold. The skill looks up that component's actuator, asks it to ``draft`` a
candidate ``treatment`` from the concerns (the actuator does its own cheap
pre-filter), and — crucially — **does not install it**. It opens a shadow A/B
experiment so the candidate is tested beside the live pipeline on real inputs over a
window before anything goes live (``advance-experiment`` runs and decides it).

A component with no registered actuator, or whose actuator declines to draft (or an
experiment already running for it), yields no effects.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from mesh_models.effect import OpenExperimentEffect
from mesh_models.improvement_experiment import ImprovementExperiment
from mesh_models.tension import Tension, TensionKind

from mesh_agents.skill import register_skill


@register_skill
class ImproveComponentSkill:
    """Ask a component's actuator to draft a fix; open a shadow experiment for it."""

    skill_id = "improve-component"
    handles = (TensionKind.improvable_component,)

    async def run(self, conn: Any, tension: Tension, *, budget_usd: float) -> list[Any]:
        from mesh_agents.actuators import get_actuator

        component = tension.target_ref.get("component", "")
        actuator = get_actuator(component)
        if actuator is None:
            return []  # no wired actuator for this component yet

        from mesh_db.improvement_concerns import list_open_concerns
        from mesh_db.improvement_experiments import get_running_experiment

        field_id = tension.field_id
        if get_running_experiment(conn, field_id, component, actuator.target) is not None:
            return []  # already under test — don't stack a second experiment

        concerns = list_open_concerns(conn, field_id, component, limit=50)
        if not concerns:
            return []

        try:
            treatment = await asyncio.to_thread(
                actuator.draft, conn, field_id, concerns
            )
        except Exception:
            return []
        if not treatment:
            return []  # actuator declined (e.g. failed its offline pre-filter)

        experiment = ImprovementExperiment(
            field_id=field_id,
            component=component,
            target=actuator.target,
            treatment=treatment,
            concern_ids=[c.id for c in concerns],
            min_sample=max(1, int(os.environ.get("MESH_EXPERIMENT_MIN_SAMPLE", "20"))),
            margin=float(os.environ.get("MESH_EXPERIMENT_MARGIN", "0.02")),
            rationale=f"accuracy concerns → {component} candidate",
        )
        return [OpenExperimentEffect(experiment=experiment)]
