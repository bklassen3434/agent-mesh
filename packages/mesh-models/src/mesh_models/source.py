from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

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
    # Phase 18a config-driven connectors (generic, user-configured sources).
    web = "web"  # web_search connector (Brave Search)
    rss = "rss"  # generic single-feed RSS/Atom connector
    rest = "rest"  # generic REST/JSON API connector
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
    # Scouted payload (title/abstract/…) the claim extractor needs. Persisted by
    # the market's scout-source skill so extract-source can recover the content a
    # round later; NULL for coordinator-written sources (extracted in one pass).
    payload: dict[str, Any] | None = None

    @field_validator("reliability_prior")
    @classmethod
    def check_reliability(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("reliability_prior must be between 0.0 and 1.0")
        return v
