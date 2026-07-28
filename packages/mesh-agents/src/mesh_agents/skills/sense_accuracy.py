"""Skill: ``sense-accuracy`` — grade the field against the web, accumulate concerns.

The barometer of the self-improvement loop. A cooldown-gated ``evaluate_accuracy``
tension (one per field, on its own long timer — the eval is expensive) routes here.
The skill samples held beliefs, grades each against the live web (the accuracy
judge), measures source freshness, and attributes every non-supported belief's
fault to ONE component (the attribution engine). Each attribution becomes an
append-only ``RecordConcernEffect`` — **the skill only accumulates evidence, it
never edits the system.** Concerns pile up across eval passes; a separate derived
tension fires the actual A/B fix once one component's concerns cross a threshold.

Degrades to no effect (retry next cooldown) when the field holds no beliefs or the
web judge is unavailable (no ANTHROPIC_API_KEY) — sensing must never break the run.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from mesh_models.effect import RecordConcernEffect
from mesh_models.tension import Tension, TensionKind

from mesh_agents.skill import register_skill


@register_skill
class SenseAccuracySkill:
    """Web-ground a sample of held beliefs → append-only fault-attribution concerns."""

    skill_id = "sense-accuracy"
    handles = (TensionKind.evaluate_accuracy,)

    def __init__(self, judge: Any | None = None, diagnoser: Any | None = None) -> None:
        # No-arg constructable for the registry; tests inject stubs.
        self._judge = judge
        self._diagnoser = diagnoser

    async def run(self, conn: Any, tension: Tension, *, budget_usd: float) -> list[Any]:
        from mesh_agents.eval import (
            LLMFaultDiagnoser,
            assess_accuracy,
            assess_freshness,
            attribute_report,
        )

        field_id = tension.target_ref.get("field_id") or tension.field_id
        judge = self._resolve_judge()
        if judge is None:
            return []  # no web judge available — retry next cooldown

        diagnoser = self._diagnoser or LLMFaultDiagnoser()
        sample_size = max(1, int(os.environ.get("MESH_EVAL_SAMPLE_SIZE", "15")))

        def _work() -> list[Any]:
            report = assess_accuracy(conn, field_id, judge, sample_size=sample_size)
            freshness = assess_freshness(conn, field_id)
            concerns = attribute_report(conn, report, diagnoser, freshness=freshness)
            return [RecordConcernEffect(concern=c) for c in concerns]

        try:
            return await asyncio.to_thread(_work)
        except Exception:
            return []  # a grading/attribution failure is not fatal to the controller

    def _resolve_judge(self) -> Any | None:
        if self._judge is not None:
            return self._judge
        try:
            from mesh_agents.eval import AnthropicWebSearchJudge

            return AnthropicWebSearchJudge()
        except Exception:
            return None  # no ANTHROPIC_API_KEY / provider — degrade
