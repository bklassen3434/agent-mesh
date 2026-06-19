"""Phase 1 of the agentic migration: the write gateway.

``apply_effects`` is the *only* place skills' decisions become writes. Skills
return ``Effect``s (declarative intents); this routes each to the existing typed
write functions, enforcing the store's invariants in one auditable place:

* claims are immutable — ``CreateClaimEffect`` inserts, ``SupersedeClaimEffect``
  only flips status; there is no "update claim" path;
* belief revisions are append-only — ``ReviseBeliefEffect`` writes a revision row
  (filling ``previous_*`` from the live head) *then* updates the head, exactly as
  the coordinator's synthesize node does today;
* entity merges go through the transactional ``merge_entities``;
* writes run on the caller's (writer-role) connection — coordinator-owned writes.

Effects are applied **sequentially on one connection**, so they are naturally
serialized — the future concurrent market must still funnel each target's writes
through a single applier (per-target ordering), but the contract here is simple:
hand it an ordered list, it applies them in order and returns a report.

Best-effort by default: a failing effect is recorded in ``ApplyReport.errors``
and the rest proceed (the coordinator's "one bad item never aborts the run"
philosophy). Pass ``strict=True`` to raise on the first failure instead.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from mesh_models.claim import ClaimStatus
from mesh_models.effect import (
    AddRelationshipEvidenceEffect,
    CreateBeliefEffect,
    CreateClaimEffect,
    CreateSourceEffect,
    MergeEntitiesEffect,
    OpenInvestigationEffect,
    ReviseBeliefEffect,
    SupersedeClaimEffect,
)
from mesh_models.revision import BeliefRevision
from pydantic import BaseModel, Field

from mesh_db.beliefs import create_belief, get_belief_by_id, update_belief
from mesh_db.claims import create_claim, update_claim_status
from mesh_db.connection import MeshConnection
from mesh_db.entities import merge_entities
from mesh_db.investigations import create_investigation
from mesh_db.relationships import add_relationship_evidence
from mesh_db.revisions import create_revision
from mesh_db.sources import create_source

log = structlog.get_logger()


class ApplyReport(BaseModel):
    """Counts of what the gateway wrote, plus any per-effect failures."""

    sources_created: int = 0
    claims_created: int = 0
    claims_superseded: int = 0
    beliefs_created: int = 0
    beliefs_revised: int = 0
    entities_merged: int = 0
    relationship_edges: int = 0
    investigations_opened: int = 0
    errors: list[dict[str, str]] = Field(default_factory=list)


def apply_effects(
    conn: MeshConnection,
    effects: list[Any],
    *,
    strict: bool = False,
) -> ApplyReport:
    """Apply an ordered list of ``Effect``s through the invariant-preserving
    write paths. Returns an ``ApplyReport``. Best-effort unless ``strict``."""
    report = ApplyReport()
    for effect in effects:
        try:
            _apply_one(conn, effect, report)
        except Exception as exc:
            if strict:
                raise
            report.errors.append(
                {
                    "effect_kind": getattr(effect, "kind", type(effect).__name__),
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
            )
            log.warning("effect_apply_failed", kind=getattr(effect, "kind", "?"), error=str(exc))
    return report


def _apply_one(conn: MeshConnection, effect: Any, report: ApplyReport) -> None:
    if isinstance(effect, CreateSourceEffect):
        create_source(conn, effect.source, field_id=effect.field_id)
        report.sources_created += 1

    elif isinstance(effect, CreateClaimEffect):
        create_claim(conn, effect.claim, field_id=effect.field_id)
        report.claims_created += 1

    elif isinstance(effect, SupersedeClaimEffect):
        update_claim_status(
            conn,
            effect.claim_id,
            ClaimStatus.superseded,
            superseded_by=effect.superseded_by_claim_id,
        )
        report.claims_superseded += 1

    elif isinstance(effect, CreateBeliefEffect):
        create_belief(conn, effect.belief, field_id=effect.field_id)
        report.beliefs_created += 1

    elif isinstance(effect, ReviseBeliefEffect):
        _apply_revise_belief(conn, effect)
        report.beliefs_revised += 1

    elif isinstance(effect, MergeEntitiesEffect):
        merge_entities(conn, effect.canonical_id, effect.duplicate_id)
        report.entities_merged += 1

    elif isinstance(effect, AddRelationshipEvidenceEffect):
        add_relationship_evidence(
            conn,
            effect.from_entity_id,
            effect.to_entity_id,
            effect.type,
            effect.claim_id,
            effect.confidence,
            field_id=effect.field_id,
        )
        report.relationship_edges += 1

    elif isinstance(effect, OpenInvestigationEffect):
        create_investigation(conn, effect.investigation, field_id=effect.field_id)
        report.investigations_opened += 1

    else:  # pragma: no cover — guards against an unrouted Effect kind
        raise TypeError(f"No gateway branch for effect: {type(effect).__name__}")


def _apply_revise_belief(conn: MeshConnection, effect: ReviseBeliefEffect) -> None:
    """Append-only belief revision (mirrors the coordinator's synthesize node):
    write the revision row from the live head, then update the head. Raises if the
    belief is gone (caller's best-effort wrapper records it)."""
    existing = get_belief_by_id(conn, effect.belief_id)
    if existing is None:
        raise ValueError(f"Belief {effect.belief_id} not found")

    revision = BeliefRevision(
        belief_id=effect.belief_id,
        previous_statement=existing.statement,
        new_statement=effect.new_statement,
        previous_confidence=existing.confidence,
        new_confidence=effect.new_confidence,
        trigger_claim_ids=effect.trigger_claim_ids,
        revised_by_agent=effect.revised_by_agent,
        revised_at=datetime.now(UTC),
        rationale=effect.rationale,
    )
    create_revision(conn, revision)

    head_updates: dict[str, Any] = {
        "statement": effect.new_statement,
        "confidence": effect.new_confidence,
        "last_revised_at": revision.revised_at,
        "revision_count": existing.revision_count + 1,
    }
    if effect.supporting_claim_ids is not None:
        head_updates["supporting_claim_ids"] = effect.supporting_claim_ids
    if effect.contradicting_claim_ids is not None:
        head_updates["contradicting_claim_ids"] = effect.contradicting_claim_ids
    update_belief(conn, effect.belief_id, **head_updates)
