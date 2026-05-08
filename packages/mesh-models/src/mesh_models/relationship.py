from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class Relationship(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    from_entity_id: str
    to_entity_id: str
    type: str
    evidence_claim_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
