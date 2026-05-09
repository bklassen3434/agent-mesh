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
