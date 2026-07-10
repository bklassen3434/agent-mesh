from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class FieldBrief(BaseModel):
    """One LLM-written "state of the field" narrative (append-only rows).

    Written by the ``write-field-brief`` skill on the controller's maintenance
    cooldown; the Field Overview API serves the latest per field.
    ``inputs_summary`` snapshots the counts the narrative was derived from."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    field_id: str
    narrative: str
    model: str = ""
    inputs_summary: dict[str, Any] = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
