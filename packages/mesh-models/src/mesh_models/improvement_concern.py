from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ConcernComponent(StrEnum):
    """Which pipeline stage a fault is attributed to — the actuator the improvement
    loop would change to fix it. Spans the whole pipeline (scout → extract → resolve
    → synthesize → challenge → age), because an inaccurate belief can be *any*
    stage's fault, not just a prompt's. ``other`` is the fallback for a fault that
    doesn't map to a known actuator yet."""

    scout = "scout"  # wrong/low-quality sources pulled → connector config / query
    extraction = "extraction"  # a claim misreads its source → extract-source prompt
    entity_resolution = "entity_resolution"  # wrong merge/split → resolution thresholds
    synthesis = "synthesis"  # claims fine, belief overstates → synthesize-belief prompt
    challenge = "challenge"  # a wrong belief went unchallenged → challenge/skeptic prompt
    confidence = "confidence"  # KB confident on wrong beliefs → calibration weights
    decay = "decay"  # a stale belief wasn't aged out → decay/archival thresholds
    freshness = "freshness"  # source/connector stale → the belief is outdated
    coverage = "coverage"  # nothing sourced covers the topic → un(der)verifiable
    other = "other"


class ConcernStatus(StrEnum):
    open = "open"  # accumulating; counts toward a component's activation threshold
    resolved = "resolved"  # an improve pass acted on it (promoted a change)
    dismissed = "dismissed"  # judged not actionable


class ImprovementConcern(BaseModel):
    """One accumulated fault-attribution — the self-improvement loop's unit of
    evidence, the analog of a claim for system quality.

    An accuracy/freshness eval writes these; it never edits the system. They
    persist and accumulate (append-only content; only ``status`` flips), so a noisy
    per-eval signal becomes a durable, countable body of evidence. When enough open
    concerns for one ``(field, component, target)`` cross a threshold, a derived
    tension fires an A/B — the same accumulate-then-activate pattern as every other
    tension on the board.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    field_id: str
    component: ConcernComponent
    # The actuator handle the fault points at: a skill_id ("extract-source",
    # "synthesize-belief"), a connector_id / source_type, or "" when component-wide.
    target: str = ""
    # The failing output that generated this concern + how bad it is.
    belief_id: str = ""
    verdict: str = ""  # the eval verdict that surfaced it (contradicted/…)
    severity: float = Field(default=0.0, ge=0.0, le=1.0)  # attributed accuracy loss
    summary: str = ""  # the fault in words — the gradient the proposer reads
    evidence_urls: list[str] = Field(default_factory=list)
    status: ConcernStatus = ConcernStatus.open
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None
    resolved_by: str = ""  # the prompt_version / run id that closed it
