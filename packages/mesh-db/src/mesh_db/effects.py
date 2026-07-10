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
serialized — a concurrent controller must still funnel each target's writes
through a single applier (per-target ordering), but the contract here is simple:
hand it an ordered list, it applies them in order and returns a report.

Best-effort by default: a failing effect is recorded in ``ApplyReport.errors``
and the rest proceed (the coordinator's "one bad item never aborts the run"
philosophy). Pass ``strict=True`` to raise on the first failure instead.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from mesh_models.claim import ClaimStatus
from mesh_models.effect import (
    AddRelationshipEvidenceEffect,
    AttachClaimToInvestigationEffect,
    CreateBeliefEffect,
    CreateClaimEffect,
    CreateEntityEffect,
    CreateSourceEffect,
    MergeBeliefsEffect,
    MergeEntitiesEffect,
    OpenInvestigationEffect,
    RejectEntityMergeEffect,
    ReviseBeliefEffect,
    SupersedeClaimEffect,
    UpdateInvestigationEffect,
    WriteFieldBriefEffect,
    WriteHeuristicEffect,
)
from mesh_models.investigation import InvestigationStatus
from mesh_models.revision import BeliefRevision
from pydantic import BaseModel, Field

from mesh_db.beliefs import (
    create_belief,
    get_belief_by_id,
    merge_beliefs,
    set_belief_embedding,
    update_belief,
)
from mesh_db.claims import create_claim, update_claim_status
from mesh_db.connection import MeshConnection
from mesh_db.entities import (
    create_entity,
    merge_entities,
    record_merge_rejection,
    set_entity_embedding,
)
from mesh_db.heuristics import create_heuristic, create_heuristic_revision
from mesh_db.investigations import (
    attach_claim_to_investigation,
    create_investigation,
    get_investigation_by_id,
    update_investigation,
)
from mesh_db.relationships import add_relationship_evidence
from mesh_db.revisions import create_revision
from mesh_db.sources import create_source

log = structlog.get_logger()

# Recompute a belief's confidence from its evidence signals after its claim links
# are written. Injected by the controller layer (``mesh_agents.confidence`` lives above
# mesh-db in the dependency graph, so the gateway can't import it directly). When
# ``None`` the gateway keeps the confidence the skill proposed — byte-for-byte the
# pre-injection behaviour, which the existing gateway tests rely on.
ConfidenceFn = Callable[[MeshConnection, str], float]


class ApplyReport(BaseModel):
    """Counts of what the gateway wrote, plus any per-effect failures."""

    sources_created: int = 0
    entities_created: int = 0
    claims_created: int = 0
    claims_superseded: int = 0
    beliefs_created: int = 0
    beliefs_revised: int = 0
    entities_merged: int = 0
    entity_merges_rejected: int = 0
    beliefs_merged: int = 0
    relationship_edges: int = 0
    investigations_opened: int = 0
    investigations_updated: int = 0
    investigation_claims_attached: int = 0
    heuristics_written: int = 0
    field_briefs_written: int = 0
    errors: list[dict[str, str]] = Field(default_factory=list)


def apply_effects(
    conn: MeshConnection,
    effects: list[Any],
    *,
    strict: bool = False,
    confidence_fn: ConfidenceFn | None = None,
) -> ApplyReport:
    """Apply an ordered list of ``Effect``s through the invariant-preserving
    write paths. Returns an ``ApplyReport``. Best-effort unless ``strict``.

    ``confidence_fn`` (optional) recomputes a belief's confidence from its
    evidence signals once its claim links are written — the evidence-derived
    score the coordinator's synthesize node applies (Phase 14d). When omitted the
    gateway keeps the confidence the skill proposed."""
    report = ApplyReport()
    for effect in effects:
        try:
            _apply_one(conn, effect, report, confidence_fn)
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


def _apply_one(
    conn: MeshConnection,
    effect: Any,
    report: ApplyReport,
    confidence_fn: ConfidenceFn | None = None,
) -> None:
    if isinstance(effect, CreateSourceEffect):
        create_source(conn, effect.source, field_id=effect.field_id)
        report.sources_created += 1

    elif isinstance(effect, CreateEntityEffect):
        create_entity(conn, effect.entity, field_id=effect.field_id)
        if effect.name_embedding is not None:
            set_entity_embedding(conn, effect.entity.id, effect.name_embedding)
        report.entities_created += 1

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
        if effect.statement_embedding is not None:
            set_belief_embedding(conn, effect.belief.id, effect.statement_embedding)
        # Recompute confidence from the evidence signals now that the belief (with
        # its supporting_claim_ids) exists — the view reads those claim links.
        if confidence_fn is not None:
            update_belief(
                conn, effect.belief.id, confidence=confidence_fn(conn, effect.belief.id)
            )
        report.beliefs_created += 1

    elif isinstance(effect, ReviseBeliefEffect):
        _apply_revise_belief(conn, effect, confidence_fn)
        report.beliefs_revised += 1

    elif isinstance(effect, MergeEntitiesEffect):
        merge_entities(conn, effect.canonical_id, effect.duplicate_id)
        report.entities_merged += 1

    elif isinstance(effect, WriteFieldBriefEffect):
        from mesh_models.field_brief import FieldBrief

        from mesh_db.field_briefs import create_field_brief

        create_field_brief(
            conn,
            FieldBrief(
                field_id=effect.field_id,
                narrative=effect.narrative,
                model=effect.model,
                inputs_summary=effect.inputs_summary,
            ),
        )
        report.field_briefs_written += 1

    elif isinstance(effect, RejectEntityMergeEffect):
        # Idempotent — a swarm's unioned copies collapse to one rejection row.
        record_merge_rejection(
            conn,
            effect.entity_id_a,
            effect.entity_id_b,
            field_id=effect.field_id,
            similarity=effect.similarity,
        )
        report.entity_merges_rejected += 1

    elif isinstance(effect, MergeBeliefsEffect):
        # Append-only belief consolidation: the duplicate is absorbed, not erased
        # (mirrors the Phase-19 sweep). The same evidence-derived confidence the
        # gateway computes elsewhere recomputes the canonical's score post-union.
        merge_beliefs(
            conn,
            effect.canonical_id,
            effect.duplicate_id,
            confidence_fn=confidence_fn,
        )
        report.beliefs_merged += 1

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

    elif isinstance(effect, UpdateInvestigationEffect):
        _apply_update_investigation(conn, effect)
        report.investigations_updated += 1

    elif isinstance(effect, AttachClaimToInvestigationEffect):
        attach_claim_to_investigation(conn, effect.investigation_id, effect.claim_id)
        report.investigation_claims_attached += 1

    elif isinstance(effect, WriteHeuristicEffect):
        # Append-only procedural memory: head row + genesis revision (the skill
        # built both and bound provenance; the gateway only inserts).
        create_heuristic(conn, effect.heuristic, field_id=effect.field_id)
        create_heuristic_revision(conn, effect.genesis_revision)
        report.heuristics_written += 1

    else:  # pragma: no cover — guards against an unrouted Effect kind
        raise TypeError(f"No gateway branch for effect: {type(effect).__name__}")


def _apply_update_investigation(
    conn: MeshConnection, effect: UpdateInvestigationEffect
) -> None:
    """Advance an investigation's lifecycle. Increments attempts from the live row
    (so concurrent dispatches don't clobber) and optionally sets status +
    resolved_at. Raises if the investigation is gone (best-effort wrapper records
    it)."""
    updates: dict[str, Any] = {}
    if effect.status is not None:
        updates["status"] = InvestigationStatus(effect.status)
    if effect.increment_attempts:
        inv = get_investigation_by_id(conn, effect.investigation_id)
        if inv is None:
            raise ValueError(f"Investigation {effect.investigation_id} not found")
        updates["pipeline_runs_attempted"] = inv.pipeline_runs_attempted + 1
    if effect.set_resolved_at:
        updates["resolved_at"] = datetime.now(UTC)
    if updates:
        update_investigation(conn, effect.investigation_id, **updates)


def _apply_revise_belief(
    conn: MeshConnection,
    effect: ReviseBeliefEffect,
    confidence_fn: ConfidenceFn | None = None,
) -> None:
    """Append-only belief revision (mirrors the coordinator's synthesize node):
    write the revision row from the live head, then update the head. Raises if the
    belief is gone (caller's best-effort wrapper records it).

    With ``confidence_fn``, the new claim links are applied first so the
    belief_signals view reflects them, confidence is recomputed from those
    signals, and both the revision row and the head record that derived value —
    so the audit trail and head stay consistent (Phase 14d fidelity)."""
    existing = get_belief_by_id(conn, effect.belief_id)
    if existing is None:
        raise ValueError(f"Belief {effect.belief_id} not found")

    link_updates: dict[str, Any] = {}
    if effect.supporting_claim_ids is not None:
        link_updates["supporting_claim_ids"] = effect.supporting_claim_ids
    if effect.contradicting_claim_ids is not None:
        link_updates["contradicting_claim_ids"] = effect.contradicting_claim_ids

    # Apply claim-link changes before deriving confidence (the view reads them).
    if confidence_fn is not None and effect.recompute_confidence and link_updates:
        update_belief(conn, effect.belief_id, **link_updates)

    # Maintenance revisions (decay/archival) opt out of evidence re-derivation so
    # their deliberately-set confidence survives; everything else recomputes.
    new_confidence = (
        confidence_fn(conn, effect.belief_id)
        if confidence_fn is not None and effect.recompute_confidence
        else effect.new_confidence
    )

    revision = BeliefRevision(
        belief_id=effect.belief_id,
        previous_statement=existing.statement,
        new_statement=effect.new_statement,
        previous_confidence=existing.confidence,
        new_confidence=new_confidence,
        trigger_claim_ids=effect.trigger_claim_ids,
        revised_by_agent=effect.revised_by_agent,
        revised_at=datetime.now(UTC),
        rationale=effect.rationale,
    )
    create_revision(conn, revision)

    head_updates: dict[str, Any] = {
        "statement": effect.new_statement,
        "confidence": new_confidence,
        "last_revised_at": revision.revised_at,
        "revision_count": existing.revision_count + 1,
        **link_updates,
    }
    if effect.set_not_held:
        head_updates["is_currently_held"] = False
    update_belief(conn, effect.belief_id, **head_updates)

    if effect.new_statement_embedding is not None:
        set_belief_embedding(conn, effect.belief_id, effect.new_statement_embedding)
