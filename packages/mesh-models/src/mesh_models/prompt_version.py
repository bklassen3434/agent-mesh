from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field


class PromptVersion(BaseModel):
    """One installed system-prompt version for a skill in a field (append-only rows).

    Written by the autonomous prompt-improvement loop when a candidate wins a
    held-out A/B against the live prompt. Content rows are immutable and never
    deleted; ``is_active`` is a status flag toggled at install time, so rolling
    back is re-activating an earlier row — the full lineage is always auditable.
    The scoreboard fields (``holdout_*``) record the evidence that earned the
    promotion.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    field_id: str
    skill_key: str  # e.g. "extract-source"
    prompt: str
    is_active: bool = False
    # Provenance / the A/B evidence that promoted this version.
    dataset_field: str = ""
    baseline_f1: float = 0.0
    best_f1: float = 0.0
    holdout_baseline_f1: float = 0.0
    holdout_best_f1: float = 0.0
    holdout_gain: float = 0.0
    rationale: str = ""
    proposer_tokens: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
