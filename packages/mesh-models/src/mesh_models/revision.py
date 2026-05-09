from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field


class BeliefRevision(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    belief_id: str
    previous_statement: str
    new_statement: str
    previous_confidence: float = Field(ge=0.0, le=1.0)
    new_confidence: float = Field(ge=0.0, le=1.0)
    trigger_claim_ids: list[str] = Field(default_factory=list)
    revised_by_agent: str
    revised_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    rationale: str
