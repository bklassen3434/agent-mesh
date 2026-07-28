from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field


class BeliefGradeRow(BaseModel):
    """One recorded accuracy grade for one belief (append-only rows).

    The eval writes one per belief it grades — supported ones too, not just
    failures — so the ledger is both a *coverage* record (which beliefs have been
    graded, and how recently, driving the rolling all-beliefs sweep) and an
    *accuracy time-series* (correctness over a window, the signal a shadow
    experiment's promote decision reads).
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    field_id: str
    belief_id: str
    verdict: str  # supported / partially_supported / contradicted / unverifiable
    judge_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    # Accuracy weight of this grade (supported=1, partial=0.5, else 0) — persisted
    # so windowed accuracy is a plain AVG(weight) with no re-derivation.
    weight: float = Field(default=0.0, ge=0.0, le=1.0)
    graded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
