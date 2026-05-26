from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class SourceType(StrEnum):
    arxiv = "arxiv"
    hn_post = "hn_post"
    hn_comment = "hn_comment"
    github = "github"
    twitter = "twitter"  # reserved; no scout in Phase 5 (Bluesky + Reddit cover social)
    bluesky = "bluesky"
    reddit = "reddit"
    blog = "blog"
    leaderboard = "leaderboard"
    # Synthesized by an agent (e.g. Skeptic's rationale-as-source for the
    # counter-claims it emits). url scheme is "agent://<agent_name>/<...>".
    agent_reasoning = "agent_reasoning"


class Source(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: SourceType
    url: str
    author: str | None = None
    published_at: datetime
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    raw_content_hash: str
    reliability_prior: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("reliability_prior")
    @classmethod
    def check_reliability(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("reliability_prior must be between 0.0 and 1.0")
        return v
