from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EntityType(StrEnum):
    model = "model"
    paper = "paper"
    benchmark = "benchmark"
    method = "method"
    person = "person"
    lab = "lab"
    repo = "repo"
    concept = "concept"


class Entity(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    type: EntityType
    attributes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
