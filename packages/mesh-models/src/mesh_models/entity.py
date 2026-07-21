from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EntityType(StrEnum):
    """The canonical AI/robotics entity types.

    Entity types are field-agnostic: ``Entity.type`` is a free ``str`` so any
    field can supply its own vocabulary (a hockey field uses player/team/coach,
    etc.). This enum is no longer an enforced schema — it's the *default*
    vocabulary for a field that supplies none (see ``FieldProfile.entity_types``),
    the fallback bucket (``concept``), and the keys the graph legend colors.
    Being a ``StrEnum``, every value doubles as a plain ``str``.
    """

    model = "model"
    paper = "paper"
    benchmark = "benchmark"
    method = "method"
    person = "person"
    lab = "lab"
    repo = "repo"
    concept = "concept"


# The default entity-type vocabulary (a field that names none inherits this).
DEFAULT_ENTITY_TYPES: list[str] = [e.value for e in EntityType]
# The universal fallback bucket for an untypable / unknown subject.
FALLBACK_ENTITY_TYPE: str = EntityType.concept.value


class Entity(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    # Free-form, field-supplied entity type (e.g. "model", "player", "team").
    # Not enum-constrained so the taxonomy is field-agnostic; the DB column is
    # already TEXT. ``concept`` is the universal fallback.
    type: str = FALLBACK_ENTITY_TYPE
    attributes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
