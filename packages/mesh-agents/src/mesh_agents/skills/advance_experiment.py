"""Skill: ``advance-experiment`` — drive one shadow A/B, decide when it's ripe.

A ``running_experiment`` tension routes here for each open shadow experiment. Two
modes, chosen by the experiment's state:

- **not ready** — gather a sampling batch: take a handful of real, already-ingested
  sources and run BOTH the live prompt (control) and the candidate prompt
  (treatment) on each, grade each extraction against the source, and record the two
  scores. The candidate's claims are **discarded** — nothing it produces ever enters
  the knowledge base (that's what makes it a *shadow* run). Cooldown-paced by the
  rule, because each batch is LLM-heavy.
- **ready** (both arms have ``min_sample`` samples) — decide: if the candidate beats
  the live arm by the margin, promote it (install the prompt + resolve the concerns
  that opened the experiment); otherwise reject. Either way close the experiment.

So a change only goes live after winning on real inputs over a window — never on a
single frozen-set score.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from mesh_models.effect import (
    DecideExperimentEffect,
    InstallPromptVersionEffect,
    RecordExperimentSampleEffect,
    ResolveConcernsEffect,
)
from mesh_models.improvement_experiment import ExperimentArm
from mesh_models.prompt_version import PromptVersion
from mesh_models.tension import Tension, TensionKind

from mesh_agents.skill import register_skill


@register_skill
class AdvanceExperimentSkill:
    """Sample both arms of a shadow A/B on real sources, and decide when ripe."""

    skill_id = "advance-experiment"
    handles = (TensionKind.running_experiment,)

    def __init__(self, llm: Any | None = None, judge: Any | None = None) -> None:
        self._llm, self._judge = llm, judge

    async def run(self, conn: Any, tension: Tension, *, budget_usd: float) -> list[Any]:
        from mesh_db.improvement_experiments import get_running_experiment

        field_id = tension.field_id
        component = tension.target_ref.get("component", "")
        target = tension.target_ref.get("target", "")
        exp = get_running_experiment(conn, field_id, component, target)
        if exp is None:
            return []

        if exp.ready:
            return self._decide(exp)

        # Not ready — gather a shadow sampling batch (LLM-heavy; run off-thread).
        try:
            return await asyncio.to_thread(self._sample_batch, conn, exp)
        except Exception:
            return []

    # -- decide --------------------------------------------------------------

    def _decide(self, exp: Any) -> list[Any]:
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
        version = PromptVersion(
            field_id=exp.field_id, skill_key=exp.target, prompt=exp.treatment_prompt,
            holdout_baseline_f1=c, holdout_best_f1=t, holdout_gain=t - c,
            rationale=f"shadow A/B promoted — {summary}",
        )
        return [
            DecideExperimentEffect(
                experiment_id=exp.id, promoted=True,
                rationale=f"promoted — {summary}",
            ),
            InstallPromptVersionEffect(version=version),
            ResolveConcernsEffect(
                concern_ids=list(exp.concern_ids), resolved_by=version.id
            ),
        ]

    # -- sample --------------------------------------------------------------

    def _sample_batch(self, conn: Any, exp: Any) -> list[Any]:
        from mesh_agents.claim_extractor import resolve_extraction_system
        from mesh_agents.eval.extraction import run_extraction, score_extraction
        from mesh_agents.profiles import load_profile

        examples = self._sample_sources(conn, exp.field_id)
        if not examples:
            return []
        clients = self._resolve_clients()
        if clients is None:
            return []
        llm, judge = clients
        # Control = the live/installed prompt; treatment = the candidate under test.
        control_prompt = resolve_extraction_system(
            exp.field_id, load_profile(exp.field_id), conn
        )

        effects: list[Any] = []
        for ex in examples:
            for arm, prompt in (
                (ExperimentArm.control, control_prompt),
                (ExperimentArm.treatment, exp.treatment_prompt),
            ):
                claims = run_extraction(llm, ex, system_prompt=prompt)  # discarded
                score = score_extraction(ex, claims, judge).f1
                effects.append(
                    RecordExperimentSampleEffect(
                        experiment_id=exp.id, arm=arm, score=score
                    )
                )
        return effects

    def _sample_sources(self, conn: Any, field_id: str) -> list[Any]:
        """A handful of recently-ingested sources that still carry their scouted
        text — the real inputs both arms are scored on."""
        from mesh_db.sources import get_source_payload, list_sources

        from mesh_agents.eval.extraction import ExtractionExample

        k = max(1, int(os.environ.get("MESH_EXPERIMENT_SAMPLE_BATCH", "5")))
        out: list[Any] = []
        for src in list_sources(conn, field_id=field_id, limit=k * 6):
            payload = get_source_payload(conn, src.id)
            title = (payload or {}).get("title", "")
            abstract = (payload or {}).get("abstract", "")
            if not (title or abstract):
                continue
            out.append(
                ExtractionExample(
                    id=src.id, source_type=getattr(src.type, "value", str(src.type)),
                    title=title, abstract=abstract,
                )
            )
            if len(out) >= k:
                break
        return out

    def _resolve_clients(self) -> tuple[Any, Any] | None:
        if self._llm is not None and self._judge is not None:
            return self._llm, self._judge
        try:
            from mesh_llm import make_llm_client

            from mesh_agents.eval import LLMExtractionJudge

            return self._llm or make_llm_client(), self._judge or LLMExtractionJudge()
        except Exception:
            return None
