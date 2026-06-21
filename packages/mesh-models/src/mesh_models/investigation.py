from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class InvestigationStatus(StrEnum):
    """Lifecycle states for an Investigation row.

    open      — Curator opened it; no scout has worked on it yet.
    in_progress — at least one pipeline run has dispatched it to scouts.
    resolved  — enough new claims arrived to consider the hypothesis tested.
    abandoned — N pipeline runs elapsed with no new claims.
    """

    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"
    abandoned = "abandoned"


class InvestigationOrigin(StrEnum):
    """Who opened an Investigation (Phase 22a).

    curator   — the reactive, per-belief Curator path (default; pre-Phase-22).
    skeptic   — opened during a falsification sweep.
    discovery — the proactive, whole-field discovery sweep (Phase 22).
    manual    — opened by a human.
    adjudication — opened by the deep adjudicate-contradiction skill to gather
                   corroboration before weighing a contradicted load-bearing belief.
    """

    curator = "curator"
    skeptic = "skeptic"
    discovery = "discovery"
    manual = "manual"
    adjudication = "adjudication"


class Investigation(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    # Phase 7a structured fields. `question` stays for backwards compat
    # with anything that read it; `hypothesis` is the canonical form
    # the LLM prompts use going forward.
    question: str
    hypothesis: str | None = None
    target_entity_id: str | None = None
    suggested_source_types: list[str] = Field(default_factory=list)
    opened_by_belief_id: str | None = None
    related_entity_ids: list[str] = Field(default_factory=list)
    status: InvestigationStatus = InvestigationStatus.open
    # Phase 22a provenance: who opened this and the human-readable "why".
    origin: InvestigationOrigin = InvestigationOrigin.curator
    trigger_rationale: str | None = None
    priority: float = Field(default=0.5, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None
    resolution_belief_id: str | None = None
    assigned_scout_agents: list[str] = Field(default_factory=list)
    pipeline_runs_attempted: int = 0
    collected_claim_ids: list[str] = Field(default_factory=list)
