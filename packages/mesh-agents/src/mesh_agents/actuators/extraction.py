"""Extraction actuator — auto-fix the ``extract-source`` prompt.

The first (prompt-kind) actuator: draft a revised extraction prompt from the
accumulated concerns (the accuracy gradient seeds the optimizer; the frozen held-out
A/B is a cheap pre-filter), then shadow-test it by re-running BOTH the live prompt
and the candidate on real ingested sources and grading each against its source. The
candidate's claims are never persisted. Promotion installs the new prompt version.
"""
from __future__ import annotations

import os
from typing import Any

from mesh_models.improvement_concern import ImprovementConcern
from mesh_models.improvement_experiment import ExperimentArm, ImprovementExperiment
from mesh_models.prompt_version import PromptVersion

from mesh_agents.actuators.base import register_actuator


@register_actuator
class ExtractionActuator:
    component = "extraction"
    target = "extract-source"
    uses_llm = True

    # -- draft ---------------------------------------------------------------

    def draft(
        self, conn: Any, field_id: str, concerns: list[ImprovementConcern]
    ) -> dict[str, Any] | None:
        dataset = self._load_dataset(conn, field_id)
        if dataset is None:
            return None  # no frozen dataset for this field — can't pre-filter
        clients = self._clients()
        if clients is None:
            return None
        llm, judge, proposer = clients
        from mesh_agents.eval import run_improvement

        try:
            run = run_improvement(
                llm, judge, proposer, dataset, extra_guidance=_guidance(concerns)
            )
        except Exception:
            return None
        if not run.promote:
            return None  # didn't clear the offline pre-filter — not worth a window
        return {"prompt": run.best_prompt, "pre_filter": run.reason}

    # -- shadow --------------------------------------------------------------

    def shadow_sample(
        self, conn: Any, field_id: str, exp: ImprovementExperiment
    ) -> list[tuple[ExperimentArm, float]]:
        from mesh_agents.claim_extractor import resolve_extraction_system
        from mesh_agents.eval.extraction import run_extraction, score_extraction
        from mesh_agents.profiles import load_profile

        candidate = exp.treatment.get("prompt", "")
        if not candidate:
            return []
        examples = self._sample_sources(conn, field_id)
        clients = self._clients()
        if not examples or clients is None:
            return []
        llm, judge, _ = clients
        control_prompt = resolve_extraction_system(field_id, load_profile(field_id), conn)

        out: list[tuple[ExperimentArm, float]] = []
        for ex in examples:
            for arm, prompt in (
                (ExperimentArm.control, control_prompt),
                (ExperimentArm.treatment, candidate),
            ):
                claims = run_extraction(llm, ex, system_prompt=prompt)  # discarded
                out.append((arm, score_extraction(ex, claims, judge).f1))
        return out

    # -- promote -------------------------------------------------------------

    def promote_effects(self, exp: ImprovementExperiment) -> list[Any]:
        from mesh_models.effect import InstallPromptVersionEffect

        c, t = exp.control_score or 0.0, exp.treatment_score or 0.0
        version = PromptVersion(
            field_id=exp.field_id, skill_key=exp.target,
            prompt=exp.treatment.get("prompt", ""),
            holdout_baseline_f1=c, holdout_best_f1=t, holdout_gain=t - c,
            rationale=f"shadow A/B promoted — control {c:.3f} vs treatment {t:.3f}",
        )
        return [InstallPromptVersionEffect(version=version)]

    # -- helpers -------------------------------------------------------------

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

    def _sample_sources(self, conn: Any, field_id: str) -> list[Any]:
        from mesh_db.sources import get_source_payload, list_sources

        from mesh_agents.eval.extraction import ExtractionExample

        k = max(1, int(os.environ.get("MESH_EXPERIMENT_SAMPLE_BATCH", "5")))
        out: list[Any] = []
        for src in list_sources(conn, field_id=field_id, limit=k * 6):
            payload = get_source_payload(conn, src.id) or {}
            title, abstract = payload.get("title", ""), payload.get("abstract", "")
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

    def _clients(self) -> tuple[Any, Any, Any] | None:
        try:
            from mesh_llm import make_llm_client

            from mesh_agents.eval import LLMExtractionJudge, LLMPromptProposer

            return make_llm_client(), LLMExtractionJudge(), LLMPromptProposer()
        except Exception:
            return None


def _guidance(concerns: list[ImprovementConcern]) -> str:
    """The accumulated concerns rendered as the gradient text the optimizer prepends
    to its critique — the real-world accuracy faults, most-severe first."""
    ranked = sorted(concerns, key=lambda c: c.severity, reverse=True)[:12]
    lines = [
        "REAL-WORLD ACCURACY FAULTS attributed to this component (from web-grounded "
        "grading of the live knowledge base — fix these, not just the frozen set):"
    ]
    lines += [f"- ({c.verdict}, severity {c.severity:.2f}) {c.summary}" for c in ranked]
    return "\n".join(lines)
