from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class InvestigationStatus(StrEnum):
    open = "open"
    active = "active"
    resolved = "resolved"
    abandoned = "abandoned"


class Investigation(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    question: str
    related_entity_ids: list[str] = Field(default_factory=list)
    status: InvestigationStatus = InvestigationStatus.open
    priority: float = Field(default=0.5, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None
    resolution_belief_id: str | None = None
    assigned_scout_agents: list[str] = Field(default_factory=list)
