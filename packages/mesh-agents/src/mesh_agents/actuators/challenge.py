"""Challenge actuator — auto-fix the skeptic (``challenge-belief``) prompt.

A ``challenge`` concern means a belief the web contradicts was held without ever
being flagged — the skeptic is missing real problems. This is a prompt-kind
actuator (like ``extraction``): draft a revised skeptic system prompt from the
concerns, then shadow-test it against the **belief-grade ledger** — the one signal
we already have that says which held beliefs are actually wrong.

For a batch of graded beliefs, run BOTH the live skeptic prompt (control) and the
candidate (treatment) on the belief's real evidence, memory-free so the arms are
comparable, and score each by whether its verdict agrees with the web:

- a belief the web contradicts/only-partly-supports  → the skeptic SHOULD flag it
- a belief the web supports                          → the skeptic should NOT flag it

So a candidate wins by catching the wrong beliefs the live prompt misses without
raising false alarms on the sound ones. The candidate's assessment is scored and
discarded — nothing it produces is written. Promotion installs the new prompt as
the active ``challenge-belief`` version (``resolve_skeptic_system`` picks it up).
"""
from __future__ import annotations

import os
from typing import Any

from mesh_models.improvement_concern import ImprovementConcern
from mesh_models.improvement_experiment import ExperimentArm, ImprovementExperiment
from mesh_models.prompt_version import PromptVersion

from mesh_agents.actuators.base import register_actuator

# Min graded beliefs before a shadow comparison means anything.
_MIN_LABELLED = 8
# Web weight at/above which a belief is "sound" (should NOT be flagged); below it
# the belief is wrong/shaky and the skeptic SHOULD flag it. supported=1.0,
# partially_supported=0.5, contradicted=0.0.
_SOUND_WEIGHT = 0.75

_PROPOSER_SYSTEM = """\
You optimize the SYSTEM PROMPT of a SKEPTIC agent for a research knowledge base.
The skeptic reads one held belief plus its supporting/contradicting claims and
returns a verdict (supported / weakened / contradicted) with counter-claims. Its
job is to CATCH beliefs that are actually wrong or overstated — without raising
false alarms on sound ones.

You are given the CURRENT prompt and a list of REAL MISSES: beliefs a web-grounded
judge found wrong or unverifiable that the skeptic failed to flag. Return a
COMPLETE revised system prompt (not a diff, not commentary) that would catch misses
like these — sharpen what evidence problems to look for, when to weaken vs
contradict — while preserving everything the current prompt does well and its
output contract. Do not make it trigger-happy: over-flagging correct beliefs is as
bad as missing wrong ones.

Return JSON with `prompt` (the full new system prompt) and `rationale` (one or two
sentences on what you changed and why)."""


@register_actuator
class ChallengeActuator:
    component = "challenge"
    target = "challenge-belief"
    uses_llm = True

    # -- draft ---------------------------------------------------------------

    def draft(
        self, conn: Any, field_id: str, concerns: list[ImprovementConcern]
    ) -> dict[str, Any] | None:
        if len(self._labelled(conn, field_id)) < _MIN_LABELLED:
            return None  # no way to shadow-test a candidate — don't spend a window
        current = self._current_prompt(conn, field_id)
        candidate = self._propose(current, concerns)
        if not candidate or candidate.strip() == current.strip():
            return None  # proposer declined / no change
        return {"prompt": candidate}

    # -- shadow --------------------------------------------------------------

    def shadow_sample(
        self, conn: Any, field_id: str, exp: ImprovementExperiment
    ) -> list[tuple[ExperimentArm, float]]:
        candidate = exp.treatment.get("prompt", "")
        if not candidate:
            return []
        llm = self._llm()
        if llm is None:
            return []
        control = self._current_prompt(conn, field_id)
        beliefs = self._sample_beliefs(conn, field_id)

        out: list[tuple[ExperimentArm, float]] = []
        for skeptic_input, verdict_weight in beliefs:
            should_flag = verdict_weight < _SOUND_WEIGHT
            for arm, prompt in (
                (ExperimentArm.control, control),
                (ExperimentArm.treatment, candidate),
            ):
                flagged = self._skeptic_flags(llm, skeptic_input, prompt)
                if flagged is None:
                    continue  # provider hiccup — skip this arm/belief
                out.append((arm, 1.0 if flagged == should_flag else 0.0))
        return out

    # -- promote -------------------------------------------------------------

    def promote_effects(self, exp: ImprovementExperiment) -> list[Any]:
        from mesh_models.effect import InstallPromptVersionEffect

        c, t = exp.control_score or 0.0, exp.treatment_score or 0.0
        version = PromptVersion(
            field_id=exp.field_id, skill_key=exp.target,
            prompt=exp.treatment.get("prompt", ""),
            holdout_baseline_f1=c, holdout_best_f1=t, holdout_gain=t - c,
            rationale=f"shadow A/B promoted — web agreement {c:.3f} vs {t:.3f}",
        )
        return [InstallPromptVersionEffect(version=version)]

    # -- helpers -------------------------------------------------------------

    def _current_prompt(self, conn: Any, field_id: str) -> str:
        from mesh_agents.profiles import load_profile
        from mesh_agents.skeptic import resolve_skeptic_system

        return resolve_skeptic_system(field_id, load_profile(field_id), conn)

    def _propose(self, current: str, concerns: list[ImprovementConcern]) -> str | None:
        from mesh_agents.eval.optimizer import PromptCandidate

        llm = self._llm()
        if llm is None:
            return None
        ranked = sorted(concerns, key=lambda c: c.severity, reverse=True)[:12]
        misses = "\n".join(
            f"- ({c.verdict}, severity {c.severity:.2f}) {c.summary}" for c in ranked
        )
        user = f"CURRENT PROMPT:\n{current}\n\nREAL MISSES (skeptic failed to flag):\n{misses}"
        try:
            candidate, _latency, _usage = llm.complete_with_usage(
                "challenge_prompt_proposer", _PROPOSER_SYSTEM, user, PromptCandidate
            )
        except Exception:
            return None
        return candidate.prompt if isinstance(candidate, PromptCandidate) else None

    def _sample_beliefs(self, conn: Any, field_id: str) -> list[tuple[Any, float]]:
        """(SkepticInput, web weight) for a batch of the field's graded beliefs."""
        from mesh_db.beliefs import get_belief_by_id

        from mesh_agents.skeptic import SkepticInput
        from mesh_agents.skills.challenge_belief import (
            _collect_in_scope_entities,
            _hydrate_claims,
        )
        from mesh_agents.sota_tracker import BeliefSummary

        k = max(1, int(os.environ.get("MESH_EXPERIMENT_SAMPLE_BATCH", "5")))
        out: list[tuple[Any, float]] = []
        for belief_id, weight in self._labelled(conn, field_id):
            belief = get_belief_by_id(conn, belief_id)
            if belief is None:
                continue
            supporting = _hydrate_claims(conn, list(belief.supporting_claim_ids))
            contradicting = _hydrate_claims(conn, list(belief.contradicting_claim_ids))
            out.append((
                SkepticInput(
                    belief=BeliefSummary(
                        belief_id=belief.id, topic=belief.topic,
                        statement=belief.statement, confidence=belief.confidence,
                    ),
                    supporting_claims=supporting,
                    contradicting_claims=contradicting,
                    in_scope_entities=_collect_in_scope_entities(
                        conn, supporting, contradicting
                    ),
                ),
                weight,
            ))
            if len(out) >= k:
                break
        return out

    def _skeptic_flags(self, llm: Any, skeptic_input: Any, prompt: str) -> bool | None:
        """Run the skeptic with ``prompt`` (memory-free) and report whether it
        flagged the belief (weakened/contradicted). None on provider failure."""
        from mesh_agents.skeptic import challenge_belief_pure

        try:
            assessment, _usage, _model = challenge_belief_pure(
                llm, skeptic_input, memory_block="", profile=None,
                system_override=prompt,
            )
        except Exception:
            return None
        return assessment.verdict in {"weakened", "contradicted"}

    def _labelled(self, conn: Any, field_id: str) -> list[tuple[str, float]]:
        try:
            from mesh_db.belief_grades import recent_grades

            return recent_grades(conn, field_id, limit=200)
        except Exception:
            return []

    def _llm(self) -> Any | None:
        try:
            from mesh_llm import make_routed_llm_client

            llm = make_routed_llm_client(agent_name="skeptic")
            llm.health_check()
            return llm
        except Exception:
            try:
                from mesh_llm import make_llm_client

                return make_llm_client()
            except Exception:
                return None
