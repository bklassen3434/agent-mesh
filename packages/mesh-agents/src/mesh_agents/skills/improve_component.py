"""Skill: ``improve-component`` — draft a fix and OPEN a shadow experiment for it.

The actuator of the self-improvement loop. A derived ``improvable_component``
tension fires here only once one component's accumulated fault-attribution concerns
crossed the activation threshold — so this runs on *evidence*, not a timer. The
skill reads those concerns (the accuracy gradient), drafts a candidate fix, cheaply
pre-filters it, and — crucially — **does not install it**. It opens a shadow A/B
experiment (``OpenExperimentEffect``) so the candidate is tested beside the live
pipeline on real inputs over a window before anything goes live (the
``advance-experiment`` skill runs and decides it).

Today the wired actuator is **extraction**: the concern summaries seed the prompt
optimizer (``run_improvement``), which hill-climbs a revised extract-source prompt
and held-out-A/Bs it on the frozen dataset — a **cheap pre-filter** so we only spend
a live shadow window on a candidate that at least beats the live prompt offline. If
it doesn't clear the pre-filter (or an experiment is already running for this
component), the skill returns no effects.

Concerns for other components (synthesis / freshness / …) still accumulate but don't
auto-fire yet — their actuators aren't built. The gradient is general; actuators grow.
"""
from __future__ import annotations

import asyncio
from typing import Any

from mesh_models.effect import OpenExperimentEffect
from mesh_models.improvement_concern import ConcernComponent
from mesh_models.improvement_experiment import ImprovementExperiment
from mesh_models.tension import Tension, TensionKind

from mesh_agents.skill import register_skill

# Component → the frozen-dataset A/B actuator. Only these auto-fire (the tension
# producer already filters to them; this is the skill-side guard).
_PROMPT_ACTUATORS = {ConcernComponent.extraction.value: "extract-source"}


@register_skill
class ImproveComponentSkill:
    """Draft + held-out-A/B a component fix; promote + resolve concerns iff it wins."""

    skill_id = "improve-component"
    handles = (TensionKind.improvable_component,)

    def __init__(
        self, llm: Any | None = None, judge: Any | None = None, proposer: Any | None = None
    ) -> None:
        self._llm, self._judge, self._proposer = llm, judge, proposer

    async def run(self, conn: Any, tension: Tension, *, budget_usd: float) -> list[Any]:
        component = tension.target_ref.get("component", "")
        skill_key = _PROMPT_ACTUATORS.get(component)
        if skill_key is None:
            return []  # no wired actuator for this component yet

        from mesh_db.improvement_concerns import list_open_concerns
        from mesh_db.improvement_experiments import get_running_experiment

        field_id = tension.field_id
        if get_running_experiment(conn, field_id, component, skill_key) is not None:
            return []  # already under test — don't stack a second experiment

        concerns = list_open_concerns(conn, field_id, component, limit=50)
        if not concerns:
            return []

        dataset = self._load_dataset(conn, field_id)
        if dataset is None:
            return []  # no frozen dataset for this field — can't A/B (yet)

        clients = self._resolve_clients()
        if clients is None:
            return []  # LLM unavailable — retry when concerns re-fire
        llm, judge, proposer = clients
        guidance = _concern_guidance(concerns)

        from mesh_agents.eval import run_improvement

        def _work() -> Any:
            return run_improvement(
                llm, judge, proposer, dataset, extra_guidance=guidance
            )

        try:
            run = await asyncio.to_thread(_work)
        except Exception:
            return []
        if not run.promote:
            return []  # didn't clear the offline pre-filter — not worth a live window

        import os

        # Open a shadow experiment — the candidate is tested beside prod, not
        # installed. It goes live only if advance-experiment promotes it.
        experiment = ImprovementExperiment(
            field_id=field_id,
            component=component,
            target=skill_key,
            treatment_prompt=run.best_prompt,
            concern_ids=[c.id for c in concerns],
            min_sample=max(1, int(os.environ.get("MESH_EXPERIMENT_MIN_SAMPLE", "20"))),
            margin=float(os.environ.get("MESH_EXPERIMENT_MARGIN", "0.02")),
            rationale=f"accuracy concerns → offline pre-filter {run.reason}",
        )
        return [OpenExperimentEffect(experiment=experiment)]

    def _load_dataset(self, conn: Any, field_id: str) -> Any | None:
        from mesh_agents.eval import load_dataset

        slug = field_id
        try:
            from mesh_db.fields import get_field

            fld = get_field(conn, field_id)
            if fld is not None:
                slug = fld.slug
        except Exception:
            pass
        try:
            return load_dataset(slug)
        except Exception:
            return None

    def _resolve_clients(self) -> tuple[Any, Any, Any] | None:
        if self._llm is not None and self._judge is not None and self._proposer is not None:
            return self._llm, self._judge, self._proposer
        try:
            from mesh_llm import make_llm_client

            from mesh_agents.eval import LLMExtractionJudge, LLMPromptProposer

            return (
                self._llm or make_llm_client(),
                self._judge or LLMExtractionJudge(),
                self._proposer or LLMPromptProposer(),
            )
        except Exception:
            return None


def _concern_guidance(concerns: list[Any]) -> str:
    """Render the accumulated concerns into the gradient text the optimizer prepends
    to its critique — the real-world accuracy faults, most-severe first."""
    ranked = sorted(concerns, key=lambda c: c.severity, reverse=True)[:12]
    lines = [
        "REAL-WORLD ACCURACY FAULTS attributed to this component (from web-grounded "
        "grading of the live knowledge base — fix these, not just the frozen set):"
    ]
    for c in ranked:
        lines.append(f"- ({c.verdict}, severity {c.severity:.2f}) {c.summary}")
    return "\n".join(lines)
