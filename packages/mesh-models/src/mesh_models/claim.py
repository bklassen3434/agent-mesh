from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ClaimStatus(StrEnum):
    active = "active"
    superseded = "superseded"
    retracted = "retracted"
    disputed = "disputed"


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
