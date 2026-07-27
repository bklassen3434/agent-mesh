"""Tests for the prompt-gradient-descent optimizer (mesh_agents.eval.optimizer).

No LLM/DB: the extractor client, the judge, and the proposer are stubs, so we
test the pure loop logic — the textual gradient (build_critique), accept/reject
by min_delta, best-so-far selection, patience early-stop, and the trajectory
handed to the proposer.
"""
from __future__ import annotations

from typing import Any

from mesh_agents.claim_extractor import (
    ClaimExtractionResult,
    ClaimObject,
    ExtractedClaim,
)
from mesh_agents.eval.extraction import (
    ClaimVerdict,
    DatasetScore,
    ExtractionDataset,
    ExtractionExample,
    ExtractionScore,
    ExtractionVerdict,
)
from mesh_agents.eval.optimizer import (
    PromptCandidate,
    TrajectoryPoint,
    build_critique,
    optimize_prompt,
)


def _claim(predicate: str, **kw: Any) -> ExtractedClaim:
    obj = ClaimObject(**kw.pop("object", {}))
    return ExtractedClaim(
        predicate=predicate,  # type: ignore[arg-type]
        subject_name=kw.pop("subject_name", "Subj"),
        raw_excerpt=kw.pop("raw_excerpt", "some excerpt"),
        object=obj,
        **kw,
    )


# --------------------------------------------------------------------------
# build_critique — the textual gradient (pure function)
# --------------------------------------------------------------------------


def _score_with(**per_ex: Any) -> DatasetScore:
    s = ExtractionScore(example_id="e1", n_claims=1, n_well_formed=1, n_grounded=1,
                        coverage=1.0, **per_ex)
    return DatasetScore(
        dataset_field="f", n_examples=1, mean_f1=0.5, mean_precision=0.5,
        mean_coverage=0.5, mean_well_formed_rate=0.9, per_example=[s],
    )


def test_critique_names_the_three_failure_classes() -> None:
    score = _score_with(
        malformed_predicates=["achieves_score", "developed_by"],
        ungrounded_excerpts=["the team went to the moon"],
        missed_facts=["Matthews scored 60 goals"],
    )
    text = build_critique(score)
    assert "EMPTY ATTRIBUTION" in text
    assert "achieves_score" in text
    assert "HALLUCINATION" in text
    assert "the team went to the moon" in text
    assert "MISSED COVERAGE" in text
    assert "Matthews scored 60 goals" in text
    # headline score line is present
    assert "F1=0.500" in text


def test_critique_tallies_repeated_failures() -> None:
    score = _score_with(malformed_predicates=["achieves_score", "achieves_score"])
    text = build_critique(score)
    assert "achieves_score (x2)" in text


def test_critique_clean_score_has_no_failure_bullets() -> None:
    text = build_critique(_score_with())
    assert "No concrete failures surfaced" in text


# --------------------------------------------------------------------------
# the loop — stubs that drive score by which prompt is in play
# --------------------------------------------------------------------------

_DATASET = ExtractionDataset(
    field_id="toronto-maple-leafs",
    examples=[ExtractionExample(id="a", source_type="blog", title="T", abstract="A")],
)


class _PromptKeyedLLM:
    """Returns a claim set chosen by the system prompt it's handed — lets a test
    make specific candidate prompts score higher or lower."""

    model = "stub"

    def __init__(self, by_prompt: dict[str, list[ExtractedClaim]],
                 default: list[ExtractedClaim]) -> None:
        self._by_prompt = by_prompt
        self._default = default
        self.seen_system: list[str] = []

    def complete_with_usage(
        self, name: str, system: str, user: str, response_model: Any
    ) -> tuple[Any, int, Any]:
        self.seen_system.append(system)
        claims = self._by_prompt.get(system, self._default)
        return ClaimExtractionResult(claims=claims), 10, object()


class _AllGroundedJudge:
    """Grounds every claim; coverage tracks how many claims were emitted (so more
    well-formed claims => higher coverage => higher F1)."""

    def judge(
        self, title: str, source_text: str, claims: list[dict[str, Any]]
    ) -> ExtractionVerdict:
        return ExtractionVerdict(
            claim_verdicts=[
                ClaimVerdict(index=i, grounded=True, excerpt_faithful=True)
                for i in range(len(claims))
            ],
            coverage=min(1.0, 0.3 * len(claims)),
        )


class _ScriptedProposer:
    """Yields canned candidates in order; records the trajectory it was shown."""

    def __init__(self, candidates: list[PromptCandidate]) -> None:
        self._candidates = candidates
        self._i = 0
        self.tokens = 0
        self.seen_trajectories: list[list[TrajectoryPoint]] = []
        self.seen_prompts: list[str] = []

    def propose(self, current_prompt: str, critique: str,
                trajectory: list[TrajectoryPoint]) -> PromptCandidate:
        self.seen_trajectories.append(list(trajectory))
        self.seen_prompts.append(current_prompt)
        self.tokens += 5
        cand = self._candidates[min(self._i, len(self._candidates) - 1)]
        self._i += 1
        return cand


_GOOD = [_claim("developed_by", object={"lab": "L"}),
         _claim("developed_by", object={"lab": "M"}),
         _claim("developed_by", object={"lab": "N"})]  # 3 wf -> coverage 0.9
_WEAK = [_claim("developed_by", object={"lab": "L"})]   # 1 wf -> coverage 0.3


def test_optimize_keeps_a_strictly_better_candidate() -> None:
    llm = _PromptKeyedLLM(by_prompt={"BETTER": _GOOD}, default=_WEAK)
    proposer = _ScriptedProposer([PromptCandidate(prompt="BETTER", rationale="more")])
    result = optimize_prompt(llm, _AllGroundedJudge(), proposer, _DATASET,
                             baseline_prompt="BASE", max_iters=1)
    assert result.baseline_f1 < result.best_f1
    assert result.best_prompt == "BETTER"
    assert result.improved is True
    assert result.iterations == 1
    assert result.history[0].rationale == "baseline (live prompt)"
    assert result.history[1].accepted is True
    assert result.proposer_tokens == 5


def test_optimize_rejects_a_worse_candidate_and_keeps_baseline() -> None:
    # Baseline is the strong one; the only candidate is weaker.
    llm = _PromptKeyedLLM(by_prompt={"BASE": _GOOD}, default=_WEAK)
    proposer = _ScriptedProposer([PromptCandidate(prompt="WORSE", rationale="nope")])
    result = optimize_prompt(llm, _AllGroundedJudge(), proposer, _DATASET,
                             baseline_prompt="BASE", max_iters=1)
    assert result.improved is False
    assert result.best_prompt == "BASE"
    assert result.history[1].accepted is False


def test_optimize_stops_after_patience_non_improving_rounds() -> None:
    llm = _PromptKeyedLLM(by_prompt={"BASE": _GOOD}, default=_WEAK)
    # Every proposal is weak -> never accepted -> patience should cut it short.
    proposer = _ScriptedProposer([PromptCandidate(prompt="WEAK", rationale="x")])
    result = optimize_prompt(llm, _AllGroundedJudge(), proposer, _DATASET,
                             baseline_prompt="BASE", max_iters=10, patience=2)
    assert result.iterations == 2  # stopped at patience, not max_iters
    assert result.improved is False


def test_min_delta_gates_a_marginal_gain() -> None:
    # Candidate is better but only barely; a large min_delta rejects it.
    llm = _PromptKeyedLLM(by_prompt={"CAND": _GOOD}, default=_WEAK)
    proposer = _ScriptedProposer([PromptCandidate(prompt="CAND", rationale="tiny")])
    result = optimize_prompt(llm, _AllGroundedJudge(), proposer, _DATASET,
                             baseline_prompt="BASE", max_iters=1, min_delta=0.99)
    assert result.improved is False
    assert result.best_prompt == "BASE"


def test_proposer_sees_growing_trajectory_and_current_best_prompt() -> None:
    llm = _PromptKeyedLLM(by_prompt={"B1": _GOOD}, default=_WEAK)
    proposer = _ScriptedProposer([
        PromptCandidate(prompt="B1", rationale="win"),   # accepted -> new best
        PromptCandidate(prompt="B2", rationale="weak"),  # weak -> rejected
    ])
    optimize_prompt(llm, _AllGroundedJudge(), proposer, _DATASET,
                    baseline_prompt="BASE", max_iters=2, patience=5)
    # Round 1 sees just the baseline point; round 2 sees baseline + round-1.
    assert len(proposer.seen_trajectories[0]) == 1
    assert len(proposer.seen_trajectories[1]) == 2
    # After round 1 accepted "B1", round 2 edits from that new best, not BASE.
    assert proposer.seen_prompts[0] == "BASE"
    assert proposer.seen_prompts[1] == "B1"
