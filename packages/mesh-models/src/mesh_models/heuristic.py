"""Procedural memory models (Phase 16b).

A learned, revisable, provenance-grounded *heuristic* (how-to) an agent applies
within a skill. Modeled on ``Belief`` / ``BeliefRevision``: a mutable head
(``AgentHeuristic``) with an append-only revision log
(``AgentHeuristicRevision``). Heuristics start at a low confidence and earn
trust over time, carry a TTL (``expires_at``) so stale how-to ages out, and
always link to the runs + claims that justify them.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

# Heuristics start low and earn trust as they survive revision (mirrors the
# migration default). The consolidation job seeds new candidates here.
DEFAULT_CONFIDENCE = 0.3
# Default time-to-live; the consolidation job may override per candidate.
DEFAULT_TTL_DAYS = 30


def _default_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=DEFAULT_TTL_DAYS)


class AgentHeuristic(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent: str
    skill: str
    # Optional finer scope (mirrors the optional provenance FK pattern):
    source: str | None = None
    entity_id: str | None = None
    heuristic: str
    confidence: float = Field(default=DEFAULT_CONFIDENCE, ge=0.0, le=1.0)
    # Mandatory provenance — the runs + claims that justify this heuristic.
    provenance_run_ids: list[str] = Field(default_factory=list)
    provenance_claim_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_revised_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    revision_count: int = 0
    expires_at: datetime = Field(default_factory=_default_expiry)
    is_currently_active: bool = True


class AgentHeuristicRevision(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    heuristic_id: str
    previous_heuristic: str
    new_heuristic: str
    previous_confidence: float = Field(ge=0.0, le=1.0)
    new_confidence: float = Field(ge=0.0, le=1.0)
    provenance_run_ids: list[str] = Field(default_factory=list)
    provenance_claim_ids: list[str] = Field(default_factory=list)
    revised_by_agent: str
    revised_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    rationale: str
