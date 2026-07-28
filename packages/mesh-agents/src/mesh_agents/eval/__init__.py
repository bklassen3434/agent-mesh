"""Accuracy + freshness evaluation of the live knowledge base.

The ``verify-*`` skills check *internal* consistency (no dangling refs,
append-only revisions, field isolation). They say nothing about whether the
content is *true* or *current*. This package fills that gap with two external
checks:

- **Freshness** (:mod:`.freshness`) — pure DB: how old is the newest source per
  type in a field? A silently-dead connector makes the KB confidently outdated,
  and nothing else catches it.
- **Accuracy** (:mod:`.accuracy` + :mod:`.judge`) — sample held beliefs, ground
  each against the live web via an LLM judge, and grade
  supported / contradicted / unverifiable. Turns "I spot-checked a few facts"
  into a repeatable score you can trend.

Both are read-only. The CLI entry point is ``mesh.cli eval-accuracy``.
"""
from __future__ import annotations

from mesh_agents.eval.accuracy import assess_accuracy
from mesh_agents.eval.extraction import (
    DatasetScore,
    ExtractionDataset,
    LLMExtractionJudge,
    evaluate_prompt,
    load_dataset,
)
from mesh_agents.eval.freshness import assess_freshness
from mesh_agents.eval.improve import ImprovementRun, run_improvement, split_dataset
from mesh_agents.eval.judge import AnthropicWebSearchJudge, BeliefJudge
from mesh_agents.eval.models import (
    AccuracyReport,
    BeliefGrade,
    FreshnessReport,
    FreshnessRow,
    Verdict,
)
from mesh_agents.eval.optimizer import (
    LLMPromptProposer,
    OptimizationResult,
    OptimizationStep,
    PromptCandidate,
    PromptProposer,
    TrajectoryPoint,
    build_critique,
    optimize_prompt,
)

__all__ = [
    "AccuracyReport",
    "AnthropicWebSearchJudge",
    "BeliefGrade",
    "BeliefJudge",
    "DatasetScore",
    "ExtractionDataset",
    "FreshnessReport",
    "FreshnessRow",
    "ImprovementRun",
    "LLMExtractionJudge",
    "LLMPromptProposer",
    "OptimizationResult",
    "OptimizationStep",
    "PromptCandidate",
    "PromptProposer",
    "TrajectoryPoint",
    "Verdict",
    "assess_accuracy",
    "assess_freshness",
    "build_critique",
    "evaluate_prompt",
    "load_dataset",
    "optimize_prompt",
    "run_improvement",
    "split_dataset",
]
