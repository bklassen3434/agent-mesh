from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ClaimStatus(StrEnum):
    active = "active"
    superseded = "superseded"
    retracted = "retracted"
    disputed = "disputed"


class ClaimType(StrEnum):
    """Phase 14a: derived classification of what a claim *asserts*, used to route
    synthesis (14b/c) — entity-anchored beliefs, relationship edges, or evidence
    signals. It is metadata derived from the predicate; it does not alter what the
    source said, so it does not violate claim-content immutability.

    The taxonomy is intentionally small and 1:1 with the extractor's predicate
    vocabulary (see ``PREDICATE_TO_CLAIM_TYPE``):

      score        — a benchmark/performance result ("X scores Y on Z")
      capability    — a property / what an entity can do ("X handles long context")
      comparison    — one entity vs another on an axis ("X outperforms Y on Z")
      attribution   — who made/owns an entity ("X developed by Y")
      lineage       — what an entity derives from ("X builds on Y")
      evaluation    — what an entity was tested on ("X evaluated on Z")
      reproduction  — confirms / fails to reproduce a prior result
      critique      — challenges the validity of a claim/result
      speculative   — forecast / opinion about the future
    """

    score = "score"
    capability = "capability"
    comparison = "comparison"
    attribution = "attribution"
    lineage = "lineage"
    evaluation = "evaluation"
    reproduction = "reproduction"
    critique = "critique"
    speculative = "speculative"


# Predicate → claim_type. The extractor emits a predicate (which also fixes the
# object shape); the claim_type is derived deterministically and 1:1 from it.
# Keeping this map total over the predicate vocabulary means a Claim always lands
# in a real bucket. Unknown predicates fall back to ``speculative`` — the inert
# bucket that 14b does NOT synthesize, so a surprise predicate can never silently
# mint a belief or edge.
PREDICATE_TO_CLAIM_TYPE: dict[str, ClaimType] = {
    # legacy four (pre-Phase-14 predicates)
    "achieves_score": ClaimType.score,
    "outperforms": ClaimType.comparison,
    "developed_by": ClaimType.attribution,
    "evaluated_on": ClaimType.evaluation,
    # Phase 14a additions
    "has_capability": ClaimType.capability,
    "based_on": ClaimType.lineage,
    "reproduces": ClaimType.reproduction,
    "critiques": ClaimType.critique,
    "speculates": ClaimType.speculative,
}


def claim_type_for_predicate(predicate: str) -> ClaimType:
    """Map a predicate to its claim_type, defaulting unknown predicates to the
    inert ``speculative`` bucket (never synthesized)."""
    return PREDICATE_TO_CLAIM_TYPE.get(predicate, ClaimType.speculative)


class FailureMode(StrEnum):
    """Structured taxonomy of why a Skeptic-authored counter-claim weakens
    or contradicts the belief it targets. Non-Skeptic claims leave this null.

    Phase 7 pre-work. Skeptic emits one of these alongside its free-text
    rationale so downstream analysis (DSPy training, hype/substance score)
    can group failures without re-parsing English.
    """

    unsupported_extrapolation = "unsupported_extrapolation"
    cherry_picked_evidence = "cherry_picked_evidence"
    methodological_flaw = "methodological_flaw"
    outdated_by_newer_claim = "outdated_by_newer_claim"
    contradicted_by_source = "contradicted_by_source"
    definitional_ambiguity = "definitional_ambiguity"
    other = "other"


class Claim(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    predicate: str
    # Derived (Phase 14a) routing classification. Always populated: when not given
    # explicitly it is inferred from the predicate by the validator below.
    claim_type: ClaimType = ClaimType.speculative
    subject_entity_id: str
    object: dict[str, Any]
    source_id: str
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    extracted_by_agent: str
    raw_excerpt: str
    status: ClaimStatus = ClaimStatus.active
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    superseded_by_claim_id: str | None = None
    # Only set by Skeptic-authored counter-claims. None for everything else.
    failure_mode: FailureMode | None = None

    @model_validator(mode="before")
    @classmethod
    def _derive_claim_type(cls, data: Any) -> Any:
        """Fill claim_type from predicate when the caller didn't set one, so every
        Claim carries a routing type without each call site having to compute it."""
        if isinstance(data, dict) and not data.get("claim_type") and data.get("predicate"):
            data = {**data, "claim_type": claim_type_for_predicate(str(data["predicate"]))}
        return data
