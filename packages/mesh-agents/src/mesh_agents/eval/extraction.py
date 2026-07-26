"""Offline eval harness for the ``extract-source`` prompt — the optimizer's loss.

Distinct from :mod:`.accuracy` (which scores the *live KB* end-to-end against the
web). This scores ONE skill's prompt against a *frozen dataset* of real source
texts, fast and deterministically enough to sit in a prompt-optimization inner
loop:

    evaluate_prompt(llm, judge, dataset, system_prompt) -> DatasetScore

Two signals per extraction:

- **Well-formedness** (deterministic, no LLM) — does each claim fill the keys its
  predicate requires? This is the exact "empty attribution" failure class: an
  ``achieves_score`` with no number, a ``developed_by`` with no lab. Cheap, so it
  can gate cheaply.
- **Grounded precision + coverage** (LLM judge, grounded on the *provided source
  text* — no web search, so cheap) — are the claims actually supported by the
  text, and did the extraction miss facts a good extractor should get?

Score = F1 of precision (well-formed AND grounded) and coverage — so emitting
nothing scores 0 (coverage 0), and hallucinating scores 0 (precision 0).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from mesh_llm.prompts import build_claim_extraction_system, format_extraction_user
from pydantic import BaseModel, Field

from mesh_agents.claim_extractor import ClaimExtractionResult, ExtractedClaim

_DATASETS_DIR = Path(__file__).parent / "datasets"

# Which ClaimObject keys each predicate must fill to be well-formed. Mirrors the
# extraction prompt's rule 3 ("Only emit a predicate when you can fill its
# key(s)"). The three narrative predicates carry no object keys — their evidence
# lives in raw_excerpt — so they require none.
_REQUIRED_OBJECT_KEYS: dict[str, tuple[str, ...]] = {
    "achieves_score": ("score", "benchmark"),
    "outperforms": ("compared_to",),
    "developed_by": ("lab",),
    "evaluated_on": ("benchmark",),
    "has_capability": ("capability",),
    "based_on": ("parent",),
    "reproduces": (),
    "critiques": (),
    "speculates": (),
}


class ExtractionExample(BaseModel):
    id: str
    source_type: str
    title: str
    abstract: str


class ExtractionDataset(BaseModel):
    field_id: str
    field_label: str = ""
    examples: list[ExtractionExample]


def load_dataset(name_or_path: str) -> ExtractionDataset:
    """Load a frozen dataset by bare name (from the packaged ``datasets/`` dir) or
    an explicit path."""
    path = Path(name_or_path)
    if not path.exists():
        # Field slugs use hyphens; dataset filenames use underscores.
        stem = name_or_path.replace("-", "_")
        for cand in (
            _DATASETS_DIR / f"extraction_{stem}.json",
            _DATASETS_DIR / name_or_path,
        ):
            if cand.exists():
                path = cand
                break
    data = json.loads(path.read_text(encoding="utf-8"))
    return ExtractionDataset.model_validate(data)


# --------------------------------------------------------------------------
# deterministic well-formedness
# --------------------------------------------------------------------------


def _object_value(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def is_well_formed(claim: ExtractedClaim) -> bool:
    """A claim is well-formed when it names a subject, carries a supporting
    excerpt, and fills every object key its predicate requires (a non-empty,
    non-zero value). This is the deterministic half of the loss — it catches the
    empty-attribution bug with no LLM call."""
    if not claim.subject_name.strip() or not claim.raw_excerpt.strip():
        return False
    required = _REQUIRED_OBJECT_KEYS.get(claim.predicate, ())
    obj = claim.object.model_dump()
    for key in required:
        val = _object_value(obj, key)
        if val is None or val == "" or val == 0 or val == 0.0:
            return False
    return True


# --------------------------------------------------------------------------
# LLM judge — grounded on the source text (no web)
# --------------------------------------------------------------------------


class ClaimVerdict(BaseModel):
    index: int
    grounded: bool  # the claim is supported by the source text
    excerpt_faithful: bool  # raw_excerpt is (near-)verbatim from the source


class ExtractionVerdict(BaseModel):
    claim_verdicts: list[ClaimVerdict] = Field(default_factory=list)
    coverage: float = Field(default=0.0, ge=0.0, le=1.0)  # recall proxy 0..1
    missed: list[str] = Field(default_factory=list)


class ExtractionJudge(Protocol):
    def judge(
        self, title: str, source_text: str, claims: list[dict[str, Any]]
    ) -> ExtractionVerdict:
        """Grade one extraction against its source text (no external knowledge)."""
        ...


_JUDGE_SYSTEM = """\
You grade a claim-extraction against its SOURCE TEXT — using ONLY that text, no
outside knowledge. You are given the source and a numbered list of extracted
claims (predicate, subject, object, and the excerpt the extractor cited).

For each claim return:
- grounded: true if the source text actually states or clearly implies it.
- excerpt_faithful: true if the cited excerpt is a real (near-verbatim) span of
  the source text.

Then judge COVERAGE: of the concrete, extractable factual claims a good
extractor should have pulled from this source, what fraction did these claims
capture? Return a number 0.0-1.0 and list the notable missed facts.

Be strict about grounding (a claim not in the text is not grounded) but fair
about coverage (opinion/filler text has few extractable facts, so thin sources
can still score high coverage with few claims). Return only valid JSON."""


class LLMExtractionJudge:
    """Grade extractions with the configured mesh LLM (structured output, no web).

    Cheap by construction: grounding is against the provided text, so the judge
    needs no search and the cheap tier is fine."""

    def __init__(self, llm: Any | None = None) -> None:
        if llm is None:
            from mesh_llm import make_llm_client

            llm = make_llm_client()
        self._llm = llm

    def judge(
        self, title: str, source_text: str, claims: list[dict[str, Any]]
    ) -> ExtractionVerdict:
        if not claims:
            # Nothing extracted — still ask coverage (did it miss everything?).
            user = _judge_user(title, source_text, [])
        else:
            user = _judge_user(title, source_text, claims)
        try:
            verdict, _latency, _usage = self._llm.complete_with_usage(
                "eval_extraction_judge", _JUDGE_SYSTEM, user, ExtractionVerdict
            )
        except Exception:
            return ExtractionVerdict()  # judge failure → conservative zero
        return verdict if isinstance(verdict, ExtractionVerdict) else ExtractionVerdict()


def _judge_user(title: str, source_text: str, claims: list[dict[str, Any]]) -> str:
    lines = [f"SOURCE TITLE: {title}", f"SOURCE TEXT:\n{source_text}", ""]
    if not claims:
        lines.append("EXTRACTED CLAIMS: (none)")
    else:
        lines.append("EXTRACTED CLAIMS:")
        for i, c in enumerate(claims):
            obj = {k: v for k, v in (c.get("object") or {}).items() if v not in ("", 0, 0.0)}
            lines.append(
                f"[{i}] predicate={c.get('predicate')} subject={c.get('subject_name')} "
                f"object={json.dumps(obj)} excerpt={json.dumps(c.get('raw_excerpt', ''))}"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


class ExtractionScore(BaseModel):
    example_id: str
    n_claims: int
    n_well_formed: int
    n_grounded: int  # well-formed AND grounded
    coverage: float

    @property
    def precision(self) -> float:
        """Well-formed & grounded over all claims emitted. 1.0 for an empty
        extraction (nothing wrong was said) — coverage is what penalizes silence."""
        if self.n_claims == 0:
            return 1.0
        return self.n_grounded / self.n_claims

    @property
    def f1(self) -> float:
        p, c = self.precision, self.coverage
        return 0.0 if (p + c) == 0 else 2 * p * c / (p + c)


class DatasetScore(BaseModel):
    dataset_field: str
    n_examples: int
    mean_f1: float
    mean_precision: float
    mean_coverage: float
    mean_well_formed_rate: float
    per_example: list[ExtractionScore]


def score_extraction(
    example: ExtractionExample,
    claims: list[ExtractedClaim],
    judge: ExtractionJudge,
) -> ExtractionScore:
    well_formed = [c for c in claims if is_well_formed(c)]
    verdict = judge.judge(
        example.title,
        f"{example.title}\n\n{example.abstract}",
        [c.model_dump(mode="json") for c in claims],
    )
    grounded_idx = {
        v.index for v in verdict.claim_verdicts if v.grounded and v.excerpt_faithful
    }
    n_grounded = sum(
        1 for i, c in enumerate(claims) if i in grounded_idx and is_well_formed(c)
    )
    return ExtractionScore(
        example_id=example.id,
        n_claims=len(claims),
        n_well_formed=len(well_formed),
        n_grounded=n_grounded,
        coverage=verdict.coverage,
    )


def run_extraction(
    llm: Any,
    example: ExtractionExample,
    *,
    system_prompt: str | None = None,
    field_id: str | None = None,
) -> list[ExtractedClaim]:
    """Run the extractor on one example with an arbitrary system prompt — the
    prompt-override seam the optimizer drives. With ``system_prompt=None`` it uses
    the field's live prompt (``build_claim_extraction_system``), i.e. today's
    baseline."""
    if system_prompt is None:
        from mesh_agents.profiles import load_profile

        profile = load_profile(field_id) if field_id else None
        system_prompt = build_claim_extraction_system(profile)
    user = format_extraction_user(title=example.title, abstract=example.abstract)
    try:
        result, _latency, _usage = llm.complete_with_usage(
            "eval_extract", system_prompt, user, ClaimExtractionResult
        )
    except Exception:
        return []  # a parse/provider failure scores as an empty extraction
    return list(result.claims)


def evaluate_prompt(
    llm: Any,
    judge: ExtractionJudge,
    dataset: ExtractionDataset,
    *,
    system_prompt: str | None = None,
) -> DatasetScore:
    """Score a candidate system prompt over the whole dataset. This is the scalar
    loss (``mean_f1``) the prompt optimizer maximizes."""
    scores: list[ExtractionScore] = []
    for example in dataset.examples:
        claims = run_extraction(
            llm, example, system_prompt=system_prompt, field_id=dataset.field_id
        )
        scores.append(score_extraction(example, claims, judge))

    n = len(scores) or 1
    return DatasetScore(
        dataset_field=dataset.field_id,
        n_examples=len(scores),
        mean_f1=sum(s.f1 for s in scores) / n,
        mean_precision=sum(s.precision for s in scores) / n,
        mean_coverage=sum(s.coverage for s in scores) / n,
        mean_well_formed_rate=sum(
            (s.n_well_formed / s.n_claims if s.n_claims else 1.0) for s in scores
        )
        / n,
        per_example=scores,
    )
