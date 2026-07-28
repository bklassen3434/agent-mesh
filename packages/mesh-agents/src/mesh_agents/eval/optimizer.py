"""Prompt-gradient-descent for the ``extract-source`` prompt.

This closes the loop the eval harness opened. :mod:`.extraction` gives us a
scalar loss (``DatasetScore.mean_f1``) and, per example, *why* it lost points —
malformed claims, hallucinations, missed facts. This module turns that into an
optimizer:

    baseline prompt --score--> loss + failure diagnostics
                                   |
                          build_critique()  ← the "textual gradient"
                                   |
                          proposer.propose(prompt, critique, trajectory)
                                   |                     ↑ OPRO-style scoreboard
                          candidate prompt --score--> keep iff strictly better
                                   |
                                  (loop)

It's a **critique-guided hill-climb**: the textual gradient (ProTeGi/TextGrad
lineage) tells an LLM proposer *what to fix*, and the running trajectory of
(iteration, F1) pairs (OPRO lineage) tells it *what has and hasn't worked*. We
accept a candidate only when it strictly beats the best-so-far by ``min_delta``,
and stop after ``patience`` consecutive non-improving rounds. Nothing here writes
to the live prompt — the winner is returned for a human to install.
"""
from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from mesh_agents.eval.extraction import (
    DatasetScore,
    ExtractionDataset,
    ExtractionJudge,
    _resolve_prompt,
    evaluate_prompt,
)

# How many concrete failure snippets of each kind to feed the proposer. Enough to
# be a signal, capped so the gradient stays a summary, not the whole transcript.
_MAX_SNIPPETS = 8


# --------------------------------------------------------------------------
# the textual gradient
# --------------------------------------------------------------------------


def _tallied(items: list[str], cap: int = _MAX_SNIPPETS) -> list[str]:
    """Dedupe-count ``items`` into ``"thing (xN)"`` lines, most frequent first,
    capped to ``cap`` distinct entries."""
    counts: dict[str, int] = {}
    for it in items:
        key = it.strip()
        if key:
            counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [f"{k} (x{n})" if n > 1 else k for k, n in ranked[:cap]]


def build_critique(score: DatasetScore) -> str:
    """Aggregate a dataset score's per-example failures into a compact critique —
    the *textual gradient* handed to the proposer. Pure function (no LLM), so it's
    deterministic and unit-testable.

    Names the three loss sources the harness measures — empty attribution
    (malformed), hallucination (ungrounded), and missed coverage — with concrete
    examples of each, plus the two worst examples by F1 so the proposer sees where
    the prompt fails hardest."""
    malformed = _tallied(
        [p for s in score.per_example for p in s.malformed_predicates]
    )
    ungrounded = _tallied(
        [e for s in score.per_example for e in s.ungrounded_excerpts]
    )
    missed = _tallied([m for s in score.per_example for m in s.missed_facts])

    worst = sorted(score.per_example, key=lambda s: s.f1)[:2]

    lines = [
        f"Current score: F1={score.mean_f1:.3f} "
        f"(precision={score.mean_precision:.3f}, coverage={score.mean_coverage:.3f}, "
        f"well-formed rate={score.mean_well_formed_rate:.3f}) "
        f"over {score.n_examples} examples.",
        "",
        "Failure diagnostics (the gradient — fix these to raise the score):",
    ]
    if malformed:
        lines.append(
            "- EMPTY ATTRIBUTION — claims dropped for missing a required object "
            "key (predicate: count). Tighten the rules so the model only emits "
            "these when it can fill every key:"
        )
        lines += [f"    · {m}" for m in malformed]
    if ungrounded:
        lines.append(
            "- HALLUCINATION — claims not supported by the source text (excerpt). "
            "Reinforce grounding + verbatim-excerpt rules:"
        )
        lines += [f"    · {e}" for e in ungrounded]
    if missed:
        lines.append(
            "- MISSED COVERAGE — concrete facts a good extractor should have "
            "caught but didn't. Broaden what the prompt tells the model to look "
            "for (without inviting hallucination):"
        )
        lines += [f"    · {m}" for m in missed]
    if not (malformed or ungrounded or missed):
        lines.append("- No concrete failures surfaced; refine only for robustness.")

    if worst:
        lines.append("")
        lines.append("Worst examples by F1:")
        for s in worst:
            lines.append(
                f"    · {s.example_id}: F1={s.f1:.2f} "
                f"(precision={s.precision:.2f}, coverage={s.coverage:.2f}, "
                f"{s.n_claims} claims, {s.n_well_formed} well-formed)"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# the proposer (the "optimizer LLM")
# --------------------------------------------------------------------------


class PromptCandidate(BaseModel):
    """A proposed replacement system prompt plus why the proposer expects it to
    score higher."""

    prompt: str = Field(description="The full revised extraction system prompt.")
    rationale: str = Field(
        description="What was changed and which failure it targets."
    )


class TrajectoryPoint(BaseModel):
    """One (attempt → score) pair in the optimization history — the scoreboard the
    OPRO-style proposer reads to avoid re-proposing what already failed."""

    iteration: int
    f1: float
    accepted: bool
    rationale: str = ""


class PromptProposer(Protocol):
    def propose(
        self,
        current_prompt: str,
        critique: str,
        trajectory: list[TrajectoryPoint],
    ) -> PromptCandidate:
        """Propose a better system prompt given the current one, its critique (the
        textual gradient), and the score trajectory so far."""
        ...


_PROPOSER_SYSTEM = """\
You are a prompt engineer optimizing the SYSTEM PROMPT of a claim-extraction
agent. The agent reads a source (title + text) and emits structured factual
claims, each with a predicate, a subject, a typed object, and a verbatim excerpt.
Its output is graded by an F1 of:
- PRECISION: fraction of emitted claims that are both well-formed (fill every
  object key their predicate requires) AND grounded in the source text.
- COVERAGE: fraction of the source's extractable facts the claims captured.

So the prompt must push the model to extract MORE real facts (coverage) while
saying NOTHING unsupported or half-filled (precision). These trade off — do not
fix one by wrecking the other.

You are given: the CURRENT prompt, a CRITIQUE of its measured failures (the
gradient), and the TRAJECTORY of prompts already tried with their scores. Return
a COMPLETE revised system prompt — not a diff, not commentary — that keeps
everything the current prompt does well and targets the critique. Preserve the
prompt's required output contract (predicate list, object-key rules, the
verbatim-excerpt requirement, the worked examples) unless changing it is exactly
what fixes a failure. Do not shrink or drop the examples.

Return JSON with `prompt` (the full new system prompt) and `rationale` (one or
two sentences on what you changed and why)."""


class LLMPromptProposer:
    """Propose prompts with the configured mesh LLM (structured output)."""

    def __init__(self, llm: Any | None = None) -> None:
        if llm is None:
            from mesh_llm import make_llm_client

            llm = make_llm_client()
        self._llm = llm
        self.tokens = 0

    def propose(
        self,
        current_prompt: str,
        critique: str,
        trajectory: list[TrajectoryPoint],
    ) -> PromptCandidate:
        user = _proposer_user(current_prompt, critique, trajectory)
        candidate, _latency, usage = self._llm.complete_with_usage(
            "eval_prompt_proposer", _PROPOSER_SYSTEM, user, PromptCandidate
        )
        self.tokens += _usage_tokens(usage)
        if not isinstance(candidate, PromptCandidate) or not candidate.prompt.strip():
            # Degenerate proposal — return the current prompt unchanged so the
            # round is a no-op rather than blowing up the optimizer.
            return PromptCandidate(prompt=current_prompt, rationale="(no proposal)")
        return candidate


def _proposer_user(
    current_prompt: str, critique: str, trajectory: list[TrajectoryPoint]
) -> str:
    lines = ["CURRENT PROMPT:", current_prompt, "", "CRITIQUE:", critique, ""]
    if trajectory:
        lines.append("TRAJECTORY SO FAR (iteration: F1, accepted — rationale):")
        for p in trajectory:
            mark = "kept" if p.accepted else "rejected"
            tail = f" — {p.rationale}" if p.rationale else ""
            lines.append(f"  {p.iteration}: F1={p.f1:.3f} ({mark}){tail}")
        lines.append("")
    lines.append("Return the full revised system prompt and a short rationale.")
    return "\n".join(lines)


def _usage_tokens(usage: Any) -> int:
    for attr in ("total_tokens", "tokens"):
        val = getattr(usage, attr, None)
        if isinstance(val, int):
            return val
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    return int(inp) + int(out)


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------


class OptimizationStep(BaseModel):
    iteration: int  # 0 == baseline
    f1: float
    precision: float
    coverage: float
    well_formed_rate: float
    accepted: bool  # became the new best-so-far
    rationale: str
    prompt: str


class OptimizationResult(BaseModel):
    dataset_field: str
    baseline_f1: float
    best_f1: float
    best_prompt: str
    iterations: int  # proposal rounds actually run (excludes the baseline)
    improved: bool
    proposer_tokens: int = 0
    history: list[OptimizationStep]

    @property
    def gain(self) -> float:
        return self.best_f1 - self.baseline_f1


def optimize_prompt(
    llm: Any,
    judge: ExtractionJudge,
    proposer: PromptProposer,
    dataset: ExtractionDataset,
    *,
    baseline_prompt: str | None = None,
    max_iters: int = 6,
    min_delta: float = 0.01,
    patience: int = 3,
    extra_guidance: str | None = None,
) -> OptimizationResult:
    """Hill-climb the extraction system prompt against ``dataset``.

    Scores the baseline (``baseline_prompt=None`` → the field's live prompt), then
    repeatedly asks ``proposer`` for a revision guided by the critique + trajectory
    and keeps it only when its ``mean_f1`` beats the best-so-far by ``min_delta``.
    Stops at ``max_iters`` proposal rounds or after ``patience`` consecutive
    non-improving rounds. Returns the winner and the full trajectory; writes
    nothing to the live prompt.

    ``extra_guidance`` (optional) is prepended to the critique every round — the
    seam the self-improvement loop uses to pass the *accuracy* gradient down: the
    real-world fault-attributions ("this predicate overreached its source") that the
    frozen-set F1 critique alone wouldn't surface."""
    baseline_score = evaluate_prompt(
        llm, judge, dataset, system_prompt=baseline_prompt
    )
    # The concrete text of whatever prompt we're standing on, so the proposer
    # always edits real text (not None → "the live prompt").
    best_prompt = _resolve_prompt(baseline_prompt, dataset.field_id)
    best_f1 = baseline_score.mean_f1
    best_score = baseline_score

    history = [
        OptimizationStep(
            iteration=0,
            f1=baseline_score.mean_f1,
            precision=baseline_score.mean_precision,
            coverage=baseline_score.mean_coverage,
            well_formed_rate=baseline_score.mean_well_formed_rate,
            accepted=True,
            rationale="baseline (live prompt)",
            prompt=best_prompt,
        )
    ]
    trajectory = [
        TrajectoryPoint(
            iteration=0, f1=best_f1, accepted=True, rationale="baseline"
        )
    ]

    stale = 0
    for i in range(1, max_iters + 1):
        critique = build_critique(best_score)
        if extra_guidance:
            critique = f"{extra_guidance.strip()}\n\n{critique}"
        candidate = proposer.propose(best_prompt, critique, trajectory)
        cand_score = evaluate_prompt(
            llm, judge, dataset, system_prompt=candidate.prompt
        )
        accepted = cand_score.mean_f1 > best_f1 + min_delta
        history.append(
            OptimizationStep(
                iteration=i,
                f1=cand_score.mean_f1,
                precision=cand_score.mean_precision,
                coverage=cand_score.mean_coverage,
                well_formed_rate=cand_score.mean_well_formed_rate,
                accepted=accepted,
                rationale=candidate.rationale,
                prompt=candidate.prompt,
            )
        )
        trajectory.append(
            TrajectoryPoint(
                iteration=i,
                f1=cand_score.mean_f1,
                accepted=accepted,
                rationale=candidate.rationale,
            )
        )
        if accepted:
            best_prompt = candidate.prompt
            best_f1 = cand_score.mean_f1
            best_score = cand_score
            stale = 0
        else:
            stale += 1
            if stale >= patience:
                break

    return OptimizationResult(
        dataset_field=dataset.field_id,
        baseline_f1=baseline_score.mean_f1,
        best_f1=best_f1,
        best_prompt=best_prompt,
        iterations=len(history) - 1,
        improved=best_f1 > baseline_score.mean_f1 + min_delta,
        proposer_tokens=getattr(proposer, "tokens", 0),
        history=history,
    )
