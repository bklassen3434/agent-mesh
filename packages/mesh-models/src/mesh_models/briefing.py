"""Wire models for the daily personalized briefing.

These are produced by the Personalizer agent and rendered by the wiki
``/briefing`` route. They are explicitly **wire-only** — no DB tables
back them. The Phase 5 locked decision is that briefings are computed
on demand, not stored. If that flips, this module is where the
representation lives.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


ItemType = Literal["belief", "revision", "claim"]


class PersonalizedItem(BaseModel):
    """One ranked candidate the user might care about."""

    item_type: ItemType
    item_id: str
    relevance_score: float = Field(ge=0.0, le=1.0)
    rationale: str


class BriefingSection(BaseModel):
    """A named group of ranked items (e.g. "New Beliefs")."""

    name: str
    description: str | None = None
    items: list[PersonalizedItem] = Field(default_factory=list)


class Briefing(BaseModel):
    """The full daily digest returned by ``GET /briefing``."""

    date: date
    profile_excerpt: str = ""
    sections: list[BriefingSection] = Field(default_factory=list)
