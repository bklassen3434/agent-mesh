from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

from mesh_db.pipeline_runs import PipelineRun
from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.entity import Entity
from mesh_models.relationship import Relationship
from mesh_models.revision import BeliefRevision
from mesh_models.source import Source
from pydantic import BaseModel

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class HealthResponse(BaseModel):
    status: str
    db_present: bool


class StatsResponse(BaseModel):
    entities: int
    claims: int
    beliefs: int
    sources: int
    revisions: int
    pipeline_runs: int
    last_pipeline_run_at: datetime | None
    last_pipeline_run_id: str | None


class SourceWithCount(BaseModel):
    source: Source
    claim_count: int


class ClaimWithContext(BaseModel):
    """Claim joined with its source and subject entity for display."""

    claim: Claim
    source: Source | None
    subject_entity: Entity | None


class EntityDetail(BaseModel):
    entity: Entity
    claims: list[Claim]
    relationships: list[Relationship]


class SourceDetail(BaseModel):
    source: Source
    claims: list[Claim]


class ClaimDetail(BaseModel):
    claim: Claim
    source: Source | None
    subject_entity: Entity | None


class RevisionWithTriggers(BaseModel):
    """Revision joined with its trigger claims for the timeline view."""

    revision: BeliefRevision
    trigger_claims: list[Claim]


class BeliefSignals(BaseModel):
    """Phase 7b derived signals over a belief.

    Pulled from the belief_hype_substance + belief_reproduction Postgres
    views; recomputed on read. Informational only — does not drive any
    mesh behavior in 7b.
    """

    source_type_diversity: int
    reproduction_count: int
    skeptic_counter_claim_count: int
    severe_failure_mode_count: int
    claims_last_30d: int
    hype_substance_score: float


class BeliefSignalSummary(BaseModel):
    """Just the two signals the beliefs *list* surfaces inline per row.

    The full BeliefSignals breakdown stays on the detail page; this keeps
    the list's batch lookup cheap.
    """

    belief_id: str
    hype_substance_score: float
    reproduction_count: int


class BeliefDetail(BaseModel):
    belief: Belief
    supporting_claims: list[ClaimWithContext]
    contradicting_claims: list[ClaimWithContext]
    revisions: list[RevisionWithTriggers]
    signals: BeliefSignals | None = None


class SkepticActivityItem(BaseModel):
    """One skeptic-triggered revision joined with its belief and trigger claims.

    Powers the wiki's "what the skeptic challenged this week" feed. Belief
    lets the feed link back to the entity; trigger_claims are the counter-
    claims the skeptic emitted that drove the revision.
    """

    revision: BeliefRevision
    belief: Belief
    trigger_claims: list[Claim]


__all__ = [
    "BeliefDetail",
    "BeliefSignalSummary",
    "ClaimDetail",
    "ClaimWithContext",
    "EntityDetail",
    "HealthResponse",
    "Page",
    "PipelineRun",
    "RevisionWithTriggers",
    "SkepticActivityItem",
    "SourceDetail",
    "SourceWithCount",
    "StatsResponse",
]
