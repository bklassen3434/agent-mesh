"""Tests for the extract-source offline eval harness (mesh_agents.eval.extraction).

No LLM/DB: the extractor client and the judge are stubs, so we test the pure
scoring logic — deterministic well-formedness, precision/coverage/F1 aggregation,
and the prompt-override seam.
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
    ExtractionDataset,
    ExtractionExample,
    ExtractionVerdict,
    evaluate_prompt,
    is_well_formed,
    load_dataset,
    run_extraction,
    score_extraction,
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
# well-formedness (deterministic, the empty-attribution bug detector)
# --------------------------------------------------------------------------


def test_score_claim_needs_number_and_benchmark() -> None:
    assert is_well_formed(
        _claim("achieves_score", object={"score": 72.8, "benchmark": "MMLU"})
    )
    # missing number -> empty attribution
    assert not is_well_formed(_claim("achieves_score", object={"benchmark": "MMLU"}))
    # missing benchmark
    assert not is_well_formed(_claim("achieves_score", object={"score": 72.8}))


def test_developed_by_needs_lab() -> None:
    assert is_well_formed(_claim("developed_by", object={"lab": "Meta AI"}))
    assert not is_well_formed(_claim("developed_by"))


def test_narrative_predicates_need_no_object_keys() -> None:
    # reproduces/critiques/speculates carry detail in raw_excerpt only
    assert is_well_formed(_claim("speculates", raw_excerpt="predicts a playoff berth"))
    assert is_well_formed(_claim("critiques", raw_excerpt="questions the metric"))


def test_missing_excerpt_or_subject_is_malformed() -> None:
    assert not is_well_formed(_claim("has_capability", object={"capability": "x"},
                                     raw_excerpt=""))
    assert not is_well_formed(_claim("has_capability", object={"capability": "x"},
                                     subject_name="  "))


# --------------------------------------------------------------------------
# scoring: precision / coverage / F1
# --------------------------------------------------------------------------


class _StubJudge:
    """Grounds a fixed set of claim indices; returns a fixed coverage."""

    def __init__(self, grounded_indices: set[int], coverage: float) -> None:
        self._grounded = grounded_indices
        self._coverage = coverage
        self.calls = 0

    def judge(
        self, title: str, source_text: str, claims: list[dict[str, Any]]
    ) -> ExtractionVerdict:
        self.calls += 1
        return ExtractionVerdict(
            claim_verdicts=[
                ClaimVerdict(
                    index=i, grounded=(i in self._grounded), excerpt_faithful=True
                )
                for i in range(len(claims))
            ],
            coverage=self._coverage,
        )


_EX = ExtractionExample(id="e1", source_type="blog", title="T", abstract="A")


def test_score_grounded_and_wellformed_precision() -> None:
    claims = [
        _claim("achieves_score", object={"score": 5.0, "benchmark": "B"}),  # wf
        _claim("developed_by", object={"lab": "L"}),  # wf
        _claim("achieves_score", object={"benchmark": "B"}),  # NOT wf (no score)
    ]
    # judge grounds all three, but claim 2 is malformed -> not counted grounded
    judge = _StubJudge(grounded_indices={0, 1, 2}, coverage=0.8)
    score = score_extraction(_EX, claims, judge)
    assert score.n_claims == 3
    assert score.n_well_formed == 2
    assert score.n_grounded == 2  # well-formed AND grounded
    assert abs(score.precision - 2 / 3) < 1e-9
    assert score.coverage == 0.8
    # F1(2/3, 0.8)
    assert abs(score.f1 - (2 * (2 / 3) * 0.8) / ((2 / 3) + 0.8)) < 1e-9


def test_hallucinated_claim_lowers_precision() -> None:
    claims = [
        _claim("has_capability", object={"capability": "fast"}),  # wf but ungrounded
    ]
    judge = _StubJudge(grounded_indices=set(), coverage=0.5)
    score = score_extraction(_EX, claims, judge)
    assert score.n_grounded == 0
    assert score.precision == 0.0
    assert score.f1 == 0.0  # precision 0 -> F1 0 no matter the coverage


def test_empty_extraction_scores_zero_via_coverage() -> None:
    judge = _StubJudge(grounded_indices=set(), coverage=0.0)
    score = score_extraction(_EX, [], judge)
    assert score.n_claims == 0
    assert score.precision == 1.0  # nothing wrong said...
    assert score.coverage == 0.0  # ...but everything missed
    assert score.f1 == 0.0


# --------------------------------------------------------------------------
# run_extraction prompt-override + evaluate_prompt aggregation
# --------------------------------------------------------------------------


class _StubLLM:
    """Returns canned claims; records the system prompt it was handed."""

    model = "stub"

    def __init__(self, claims: list[ExtractedClaim]) -> None:
        self._claims = claims
        self.seen_system: list[str] = []

    def complete_with_usage(
        self, name: str, system: str, user: str, response_model: Any
    ) -> tuple[Any, int, Any]:
        self.seen_system.append(system)
        return ClaimExtractionResult(claims=self._claims), 10, object()


def test_run_extraction_uses_override_prompt() -> None:
    llm = _StubLLM([_claim("developed_by", object={"lab": "L"})])
    claims = run_extraction(llm, _EX, system_prompt="CANDIDATE PROMPT")
    assert llm.seen_system == ["CANDIDATE PROMPT"]
    assert len(claims) == 1


def test_evaluate_prompt_aggregates_over_dataset() -> None:
    dataset = ExtractionDataset(
        field_id="toronto-maple-leafs",
        examples=[
            ExtractionExample(id="a", source_type="blog", title="Ta", abstract="Aa"),
            ExtractionExample(id="b", source_type="rss", title="Tb", abstract="Ab"),
        ],
    )
    llm = _StubLLM([_claim("developed_by", object={"lab": "L"})])
    judge = _StubJudge(grounded_indices={0}, coverage=1.0)
    result = evaluate_prompt(llm, judge, dataset, system_prompt="P")
    assert result.n_examples == 2
    assert result.mean_precision == 1.0
    assert result.mean_coverage == 1.0
    assert result.mean_f1 == 1.0
    # override prompt threaded to every example
    assert llm.seen_system == ["P", "P"]


# --------------------------------------------------------------------------
# the packaged frozen dataset loads and is well-shaped
# --------------------------------------------------------------------------


def test_packaged_leafs_dataset_loads() -> None:
    ds = load_dataset("toronto-maple-leafs")
    assert ds.field_id == "toronto-maple-leafs"
    assert len(ds.examples) >= 10
    for ex in ds.examples:
        assert ex.title.strip()
        assert ex.abstract.strip()
        assert ex.source_type in {"blog", "rss", "rest"}
