"""Autonomous, overfitting-safe prompt improvement — the closed loop.

:mod:`.optimizer` hill-climbs a prompt against a dataset and returns a winner, but
optimizing *and* judging on the same examples overfits: the proposer can learn the
handful of frozen examples rather than the extraction task, so its ``mean_f1``
gain says nothing about real performance. This module makes the loop honest and
hands-free — no human picks the winner, a held-out A/B does:

1. **split** the frozen dataset into a TRAIN slice and a HOLDOUT slice
   (deterministic, seeded).
2. **optimize** on TRAIN only (:func:`.optimizer.optimize_prompt`).
3. **A/B** the winner vs the live baseline on the HOLDOUT slice — examples the
   optimizer never saw. This is the accept/reject test.
4. **promote** iff the winner beats the baseline on holdout by ``promote_delta``
   *and* didn't regress well-formedness. The held-out eval is the sole judge.

:func:`run_improvement` returns an :class:`ImprovementRun` carrying the train
trajectory, the holdout A/B, a ``promote`` verdict, and the winning prompt. It
writes nothing — the caller (CLI ``--apply`` or the controller skill) installs the
prompt into the ``prompt_versions`` store iff ``promote`` is true.
"""
from __future__ import annotations

import random
from typing import Any

from pydantic import BaseModel

from mesh_agents.eval.extraction import (
    DatasetScore,
    ExtractionDataset,
    ExtractionJudge,
    _resolve_prompt,
    evaluate_prompt,
)
from mesh_agents.eval.optimizer import (
    OptimizationResult,
    PromptProposer,
    optimize_prompt,
)

# A held-out A/B needs at least one example on each side to mean anything.
_MIN_EXAMPLES_FOR_AB = 2
# Floating-point slack when comparing well-formed rates (don't fail promotion on
# a rounding-scale regression).
_WF_EPS = 1e-9


def split_dataset(
    dataset: ExtractionDataset,
    *,
    holdout_fraction: float = 0.3,
    seed: int = 0,
) -> tuple[ExtractionDataset, ExtractionDataset]:
    """Deterministically partition ``dataset`` into (train, holdout).

    Seeded so a given (dataset, fraction, seed) always yields the same split — the
    holdout is a fixed, reproducible test set, not a fresh random draw each run.
    Guarantees at least one example on each side whenever the dataset has ≥2.
    """
    n = len(dataset.examples)
    idx = list(range(n))
    random.Random(seed).shuffle(idx)
    n_holdout = max(1, round(holdout_fraction * n)) if n >= _MIN_EXAMPLES_FOR_AB else n
    # Keep at least one training example when we can.
    n_holdout = min(n_holdout, n - 1) if n >= _MIN_EXAMPLES_FOR_AB else n_holdout
    holdout_idx = set(idx[:n_holdout])

    def _sub(keep: set[int]) -> ExtractionDataset:
        return ExtractionDataset(
            field_id=dataset.field_id,
            field_label=dataset.field_label,
            examples=[e for i, e in enumerate(dataset.examples) if i in keep],
        )

    return _sub(set(idx) - holdout_idx), _sub(holdout_idx)


class ImprovementRun(BaseModel):
    """The result of one autonomous improve pass — enough to audit the decision and
    install the winner if it earned promotion."""

    dataset_field: str
    n_train: int
    n_holdout: int
    # Train-side hill-climb (baseline + candidates scored on the training slice).
    optimization: OptimizationResult
    # Held-out A/B — the honest test. Baseline vs winner on unseen examples.
    holdout_baseline_f1: float
    holdout_best_f1: float
    holdout_baseline_wf_rate: float
    holdout_best_wf_rate: float
    # The verdict + why. `promote` is the sole gate the installer checks.
    promote: bool
    reason: str
    baseline_prompt: str
    best_prompt: str
    proposer_tokens: int = 0

    @property
    def holdout_gain(self) -> float:
        return self.holdout_best_f1 - self.holdout_baseline_f1


def _no_ab_run(
    dataset: ExtractionDataset,
    baseline_text: str,
    baseline_score: DatasetScore,
    opt: OptimizationResult | None,
    reason: str,
) -> ImprovementRun:
    """Assemble a not-promoted run for the degenerate case where we can't A/B (too
    few examples to hold any out). We never promote a prompt we couldn't test."""
    return ImprovementRun(
        dataset_field=dataset.field_id,
        n_train=len(dataset.examples),
        n_holdout=0,
        optimization=opt
        or OptimizationResult(
            dataset_field=dataset.field_id,
            baseline_f1=baseline_score.mean_f1,
            best_f1=baseline_score.mean_f1,
            best_prompt=baseline_text,
            iterations=0,
            improved=False,
            history=[],
        ),
        holdout_baseline_f1=baseline_score.mean_f1,
        holdout_best_f1=baseline_score.mean_f1,
        holdout_baseline_wf_rate=baseline_score.mean_well_formed_rate,
        holdout_best_wf_rate=baseline_score.mean_well_formed_rate,
        promote=False,
        reason=reason,
        baseline_prompt=baseline_text,
        best_prompt=baseline_text,
    )


def run_improvement(
    llm: Any,
    judge: ExtractionJudge,
    proposer: PromptProposer,
    dataset: ExtractionDataset,
    *,
    baseline_prompt: str | None = None,
    holdout_fraction: float = 0.3,
    seed: int = 0,
    promote_delta: float = 0.02,
    max_iters: int = 6,
    min_delta: float = 0.01,
    patience: int = 3,
) -> ImprovementRun:
    """Optimize the extract-source prompt on a training split, then decide by a
    held-out A/B whether the winner actually beats today's prompt.

    ``baseline_prompt=None`` optimizes against the field's live prompt. Promotion
    requires the winner to (a) win the train hill-climb, (b) beat the baseline's
    holdout F1 by ``promote_delta``, and (c) not regress the holdout well-formed
    rate. Writes nothing; the caller installs iff ``promote``.
    """
    baseline_text = _resolve_prompt(baseline_prompt, dataset.field_id)
    train, holdout = split_dataset(
        dataset, holdout_fraction=holdout_fraction, seed=seed
    )

    if not train.examples or not holdout.examples:
        base_score = evaluate_prompt(
            llm, judge, dataset, system_prompt=baseline_prompt
        )
        return _no_ab_run(
            dataset,
            baseline_text,
            base_score,
            None,
            f"dataset too small for a held-out A/B (need ≥{_MIN_EXAMPLES_FOR_AB} "
            f"examples, have {len(dataset.examples)})",
        )

    opt = optimize_prompt(
        llm,
        judge,
        proposer,
        train,
        baseline_prompt=baseline_prompt,
        max_iters=max_iters,
        min_delta=min_delta,
        patience=patience,
    )

    # The A/B: score both prompts on the untouched holdout slice.
    hold_base = evaluate_prompt(llm, judge, holdout, system_prompt=baseline_text)
    hold_best = evaluate_prompt(llm, judge, holdout, system_prompt=opt.best_prompt)
    gain = hold_best.mean_f1 - hold_base.mean_f1
    wf_regressed = (
        hold_best.mean_well_formed_rate
        < hold_base.mean_well_formed_rate - _WF_EPS
    )

    if not opt.improved:
        promote, reason = False, "no candidate beat the baseline on the training set"
    elif gain < promote_delta:
        promote, reason = (
            False,
            f"held-out gain {gain:+.3f} below promote threshold {promote_delta:.3f} "
            "(train gain did not generalize)",
        )
    elif wf_regressed:
        promote, reason = (
            False,
            f"held-out well-formed rate regressed "
            f"({hold_base.mean_well_formed_rate:.3f} → "
            f"{hold_best.mean_well_formed_rate:.3f})",
        )
    else:
        promote, reason = (
            True,
            f"held-out F1 {hold_base.mean_f1:.3f} → {hold_best.mean_f1:.3f} "
            f"({gain:+.3f} ≥ {promote_delta:.3f}) on {len(holdout.examples)} unseen "
            "examples",
        )

    return ImprovementRun(
        dataset_field=dataset.field_id,
        n_train=len(train.examples),
        n_holdout=len(holdout.examples),
        optimization=opt,
        holdout_baseline_f1=hold_base.mean_f1,
        holdout_best_f1=hold_best.mean_f1,
        holdout_baseline_wf_rate=hold_base.mean_well_formed_rate,
        holdout_best_wf_rate=hold_best.mean_well_formed_rate,
        promote=promote,
        reason=reason,
        baseline_prompt=baseline_text,
        best_prompt=opt.best_prompt,
        proposer_tokens=opt.proposer_tokens,
    )
