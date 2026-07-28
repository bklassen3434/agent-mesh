"""Skill: ``sense-accuracy`` — grade the field against the web, accumulate concerns.

The barometer of the self-improvement loop. A cooldown-gated ``evaluate_accuracy``
tension routes here. Each pass grades the ``MESH_EVAL_SAMPLE_SIZE`` *least-recently-
graded* held beliefs against the live web (a rolling sweep — over successive passes
**every** belief is graded, then re-graded oldest-first; the grade ledger is the
cursor), measures source freshness, and attributes every non-supported belief's
fault to ONE component (the attribution engine).

It emits two append-only effects and **edits nothing**: a ``RecordGradeEffect`` for
*every* belief graded (supported ones too — the ledger is a coverage record + an
accuracy time-series) and a ``RecordConcernEffect`` per fault. Concerns pile up
across passes; a separate tension fires the actual fix once they cross a threshold.

Degrades to no effect (retry next cooldown) when the field holds no beliefs or the
web judge is unavailable (no ANTHROPIC_API_KEY) — sensing must never break the run.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from mesh_models.belief_grade import BeliefGradeRow
from mesh_models.effect import RecordConcernEffect, RecordGradeEffect
from mesh_models.tension import Tension, TensionKind

from mesh_agents.skill import register_skill

# Accuracy weight per verdict (mirrors AccuracyReport.accuracy_score): the ledger
# stores it so windowed accuracy is a plain average.
_VERDICT_WEIGHT = {
    "supported": 1.0,
    "partially_supported": 0.5,
    "contradicted": 0.0,
    "unverifiable": 0.0,
}


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
        batch = max(1, int(os.environ.get("MESH_EVAL_SAMPLE_SIZE", "15")))

        def _work() -> list[Any]:
            report = assess_accuracy(
                conn, field_id, judge, sample_size=batch, strategy="coverage"
            )
            freshness = assess_freshness(conn, field_id)
            effects: list[Any] = [
                RecordGradeEffect(
                    grade=BeliefGradeRow(
                        field_id=report.field_id,
                        belief_id=g.belief_id,
                        verdict=g.verdict.value,
                        judge_confidence=g.judge_confidence,
                        weight=_VERDICT_WEIGHT.get(g.verdict.value, 0.0),
                    )
                )
                for g in report.grades
            ]
            concerns = attribute_report(conn, report, diagnoser, freshness=freshness)
            effects.extend(RecordConcernEffect(concern=c) for c in concerns)
            return effects

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
