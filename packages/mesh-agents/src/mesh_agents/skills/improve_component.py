"""Skill: ``improve-component`` — A/B a fix for a component, promote iff it wins.

The actuator of the self-improvement loop. A derived ``improvable_component``
tension fires here only once one component's accumulated fault-attribution concerns
crossed the activation threshold — so this runs on *evidence*, not a timer. The
skill reads those concerns (the accuracy gradient), drafts a fix, and validates it
with a held-out A/B before promoting.

Today the wired actuator is **extraction**: the concern summaries seed the prompt
optimizer (``run_improvement``), which hill-climbs a revised extract-source prompt
on a training split and A/Bs it on held-out examples. It promotes — installs the
new prompt (``InstallPromptVersionEffect``) and resolves the concerns
(``ResolveConcernsEffect``) — **only if the candidate actually beats the live
prompt** on the holdout. If it doesn't, the skill returns no effects; the
controller's stall cooldown backs the tension off until more concerns accrue.

Concerns for other components (synthesis / freshness / coverage / confidence) still
accumulate and surface, but don't auto-fire yet — their A/B actuators aren't built.
The gradient is general; the actuators grow over time.
"""
from __future__ import annotations

import asyncio
from typing import Any

from mesh_models.effect import InstallPromptVersionEffect, ResolveConcernsEffect
from mesh_models.improvement_concern import ConcernComponent
from mesh_models.prompt_version import PromptVersion
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

        field_id = tension.field_id
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
            return []  # A/B didn't beat the live prompt — stall cooldown backs off

        version = PromptVersion(
            field_id=field_id,
            skill_key=skill_key,
            prompt=run.best_prompt,
            dataset_field=run.dataset_field,
            baseline_f1=run.optimization.baseline_f1,
            best_f1=run.optimization.best_f1,
            holdout_baseline_f1=run.holdout_baseline_f1,
            holdout_best_f1=run.holdout_best_f1,
            holdout_gain=run.holdout_gain,
            rationale=f"accuracy concerns → {run.reason}",
            proposer_tokens=run.proposer_tokens,
        )
        return [
            InstallPromptVersionEffect(version=version),
            ResolveConcernsEffect(
                concern_ids=[c.id for c in concerns], resolved_by=version.id
            ),
        ]

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
