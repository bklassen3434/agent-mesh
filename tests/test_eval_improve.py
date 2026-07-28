"""Tests for the autonomous, overfitting-safe improvement loop + its prompt store.

Two halves:

- **No LLM/DB** — the extractor client, judge, and proposer are stubs, so we test
  the honest-A/B logic: the deterministic split, promotion when a winner
  generalizes to the held-out slice, and rejection when it only overfit the train
  slice (the whole point of holding data out).
- **DB (testcontainers)** — the append-only ``prompt_versions`` store and the live
  extractor's override resolution.
"""
from __future__ import annotations

from typing import Any

from mesh_agents.claim_extractor import (
    ClaimExtractionResult,
    ClaimObject,
    ExtractedClaim,
    resolve_extraction_system,
)
from mesh_agents.eval.extraction import (
    ClaimVerdict,
    ExtractionDataset,
    ExtractionExample,
    ExtractionVerdict,
)
from mesh_agents.eval.improve import run_improvement, split_dataset
from mesh_agents.eval.optimizer import PromptCandidate, TrajectoryPoint
from mesh_llm.prompts import build_claim_extraction_system


def _claim(lab: str) -> ExtractedClaim:
    return ExtractedClaim(
        predicate="developed_by",
        subject_name="Subj",
        raw_excerpt="some excerpt",
        object=ClaimObject(lab=lab),
    )


_GOOD = [_claim("L"), _claim("M"), _claim("N")]  # 3 well-formed -> high coverage
_WEAK = [_claim("L")]  # 1 well-formed -> low coverage


def _dataset(n: int) -> ExtractionDataset:
    return ExtractionDataset(
        field_id="toronto-maple-leafs",
        examples=[
            ExtractionExample(
                id=f"e{i}", source_type="blog",
                title=f"Example{i}", abstract=f"body {i}",
            )
            for i in range(n)
        ],
    )


class _ExampleAwareLLM:
    """Return a claim set chosen by ``good_for(system, user)`` — lets a test make a
    prompt score well on some examples (identified by their title in the user
    message) and poorly on others, i.e. simulate overfitting."""

    model = "stub"

    def __init__(self, good_for: Any) -> None:
        self._good_for = good_for

    def complete_with_usage(
        self, name: str, system: str, user: str, response_model: Any
    ) -> tuple[Any, int, Any]:
        claims = _GOOD if self._good_for(system, user) else _WEAK
        return ClaimExtractionResult(claims=claims), 10, object()


class _AllGroundedJudge:
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
    def __init__(self, prompt: str, rationale: str = "x") -> None:
        self._cand = PromptCandidate(prompt=prompt, rationale=rationale)
        self.tokens = 0

    def propose(
        self, current_prompt: str, critique: str, trajectory: list[TrajectoryPoint]
    ) -> PromptCandidate:
        self.tokens += 5
        return self._cand


# --------------------------------------------------------------------------
# split_dataset — deterministic, disjoint, non-empty
# --------------------------------------------------------------------------


def test_split_is_deterministic_disjoint_and_covers_everything() -> None:
    ds = _dataset(10)
    a_train, a_hold = split_dataset(ds, holdout_fraction=0.3, seed=0)
    b_train, b_hold = split_dataset(ds, holdout_fraction=0.3, seed=0)
    hold_ids = {e.id for e in a_hold.examples}
    train_ids = {e.id for e in a_train.examples}
    # reproducible
    assert hold_ids == {e.id for e in b_hold.examples}
    assert train_ids == {e.id for e in b_train.examples}
    # disjoint + total
    assert hold_ids & train_ids == set()
    assert hold_ids | train_ids == {e.id for e in ds.examples}
    assert len(a_hold.examples) == 3  # round(0.3 * 10)
    assert a_train.field_id == ds.field_id  # field carried onto the slice


def test_split_keeps_one_on_each_side_for_a_tiny_dataset() -> None:
    train, hold = split_dataset(_dataset(2), holdout_fraction=0.3, seed=0)
    assert len(train.examples) == 1 and len(hold.examples) == 1


# --------------------------------------------------------------------------
# run_improvement — the honest A/B decides promotion
# --------------------------------------------------------------------------


def test_promotes_a_winner_that_generalizes_to_the_holdout() -> None:
    # "BETTER" extracts the full claim set on EVERY example (train and holdout).
    llm = _ExampleAwareLLM(lambda system, user: system == "BETTER")
    run = run_improvement(
        llm, _AllGroundedJudge(), _ScriptedProposer("BETTER"), _dataset(6),
        baseline_prompt="BASE", seed=0, max_iters=1,
    )
    assert run.optimization.improved is True
    assert run.holdout_best_f1 > run.holdout_baseline_f1
    assert run.promote is True
    assert run.best_prompt == "BETTER"
    assert run.n_train and run.n_holdout  # a real split happened
    assert run.proposer_tokens == 5


def test_rejects_a_winner_that_only_overfit_the_train_slice() -> None:
    ds = _dataset(6)
    train, _hold = split_dataset(ds, holdout_fraction=0.3, seed=0)
    train_titles = {e.title for e in train.examples}
    # "OVERFIT" wins on train examples only; on the held-out examples it is no
    # better than the baseline — so the A/B must refuse to promote it.
    llm = _ExampleAwareLLM(
        lambda system, user: system == "OVERFIT"
        and any(t in user for t in train_titles)
    )
    run = run_improvement(
        llm, _AllGroundedJudge(), _ScriptedProposer("OVERFIT"), ds,
        baseline_prompt="BASE", seed=0, max_iters=1, promote_delta=0.02,
    )
    assert run.optimization.improved is True  # it did win the training climb
    assert run.holdout_best_f1 <= run.holdout_baseline_f1 + 0.02
    assert run.promote is False
    assert "generalize" in run.reason
    assert run.best_prompt == "OVERFIT"  # still returned, just not promoted


def test_too_small_to_ab_never_promotes() -> None:
    llm = _ExampleAwareLLM(lambda system, user: True)
    run = run_improvement(
        llm, _AllGroundedJudge(), _ScriptedProposer("X"), _dataset(1),
        baseline_prompt="BASE",
    )
    assert run.promote is False
    assert run.n_holdout == 0
    assert "too small" in run.reason


# --------------------------------------------------------------------------
# prompt_versions store (DB) — append-only install / active lookup / lineage
# --------------------------------------------------------------------------


def _reset_store(conn: Any, skill_key: str) -> str:
    from mesh_models.field import DEFAULT_FIELD_ID

    conn.execute(
        "DELETE FROM prompt_versions WHERE skill_key = %s", [skill_key]
    )
    return DEFAULT_FIELD_ID


def test_install_activates_and_deactivates_prior_keeping_all_rows(tmp_db: Any) -> None:
    from mesh_db.prompt_versions import (
        get_active_prompt,
        install_prompt_version,
        list_prompt_versions,
    )
    from mesh_models.prompt_version import PromptVersion

    skill = "extract-source-test"
    field_id = _reset_store(tmp_db, skill)

    v1 = install_prompt_version(
        tmp_db,
        PromptVersion(field_id=field_id, skill_key=skill, prompt="PROMPT-1"),
    )
    assert get_active_prompt(tmp_db, field_id, skill) == "PROMPT-1"

    v2 = install_prompt_version(
        tmp_db,
        PromptVersion(
            field_id=field_id, skill_key=skill, prompt="PROMPT-2",
            holdout_gain=0.12,
        ),
    )
    # newest is active; the prior content row is retained (append-only), just off.
    assert get_active_prompt(tmp_db, field_id, skill) == "PROMPT-2"
    lineage = list_prompt_versions(tmp_db, field_id, skill)
    assert [v.id for v in lineage] == [v2.id, v1.id]  # newest first
    assert [v.is_active for v in lineage] == [True, False]
    assert lineage[0].holdout_gain == 0.12

    _reset_store(tmp_db, skill)


def test_get_active_prompt_is_none_when_nothing_installed(tmp_db: Any) -> None:
    from mesh_db.prompt_versions import get_active_prompt

    field_id = _reset_store(tmp_db, "extract-source-test")
    assert get_active_prompt(tmp_db, field_id, "extract-source-test") is None


# --------------------------------------------------------------------------
# live wiring — the extractor prefers an installed prompt, else the built-in
# --------------------------------------------------------------------------


def test_resolve_extraction_system_prefers_installed_override(tmp_db: Any) -> None:
    from mesh_db.prompt_versions import install_prompt_version
    from mesh_models.field import DEFAULT_FIELD_ID
    from mesh_models.prompt_version import PromptVersion

    tmp_db.execute(
        "DELETE FROM prompt_versions WHERE field_id = %s AND skill_key = 'extract-source'",
        [DEFAULT_FIELD_ID],
    )
    # No override installed -> the built-in prompt.
    built_in = build_claim_extraction_system(None)
    assert resolve_extraction_system(DEFAULT_FIELD_ID, None, tmp_db) == built_in

    install_prompt_version(
        tmp_db,
        PromptVersion(
            field_id=DEFAULT_FIELD_ID, skill_key="extract-source",
            prompt="INSTALLED EXTRACTION PROMPT",
        ),
    )
    assert (
        resolve_extraction_system(DEFAULT_FIELD_ID, None, tmp_db)
        == "INSTALLED EXTRACTION PROMPT"
    )

    tmp_db.execute(
        "DELETE FROM prompt_versions WHERE field_id = %s AND skill_key = 'extract-source'",
        [DEFAULT_FIELD_ID],
    )
