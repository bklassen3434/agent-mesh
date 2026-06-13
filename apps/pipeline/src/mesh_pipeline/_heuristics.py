"""Coordinator-side heuristic persistence (Phase 16b).

Mirrors ``_investigations.persist_investigation_suggestions``: agents (and the
consolidation job) propose ``HeuristicProposal``s; this module — running under
the coordinator-writer role — validates and persists them into the procedural
store, writing both the head row and an append-only revision row. No agent role
writes these tables directly (verified by role in the tests).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from mesh_agents.consolidator import HeuristicProposal
from mesh_db.heuristics import (
    create_heuristic,
    create_heuristic_revision,
    get_heuristic_by_id,
    update_heuristic,
)
from mesh_models.field import DEFAULT_FIELD_ID
from mesh_models.heuristic import AgentHeuristic, AgentHeuristicRevision


class MissingProvenanceError(ValueError):
    """A heuristic was proposed with no justifying run or claim. Provenance is
    mandatory, so the coordinator refuses to persist it."""


def persist_heuristic(
    conn: Any,
    proposal: HeuristicProposal,
    *,
    revised_by_agent: str = "consolidator",
    now: datetime | None = None,
    field_id: str = DEFAULT_FIELD_ID,
) -> AgentHeuristic:
    """Persist a *new* heuristic: head row + a genesis revision (append-only).

    The genesis revision records creation from nothing (previous_* empty/zero),
    so the revision log is a complete history from the first persist. Raises
    ``MissingProvenanceError`` when the proposal carries no provenance. The
    heuristic is scoped to ``field_id`` — it never leaks to another field."""
    if not proposal.has_provenance():
        raise MissingProvenanceError(
            f"heuristic for {proposal.agent}/{proposal.skill} has no provenance"
        )
    now = now or datetime.now(UTC)
    heuristic = AgentHeuristic(
        agent=proposal.agent,
        skill=proposal.skill,
        source=proposal.source,
        entity_id=proposal.entity_id,
        heuristic=proposal.heuristic,
        confidence=proposal.confidence,
        provenance_run_ids=proposal.provenance_run_ids,
        provenance_claim_ids=proposal.provenance_claim_ids,
        created_at=now,
        last_revised_at=now,
        revision_count=0,
        expires_at=now + timedelta(days=proposal.ttl_days),
        is_currently_active=True,
    )
    create_heuristic(conn, heuristic, field_id=field_id)
    create_heuristic_revision(
        conn,
        AgentHeuristicRevision(
            heuristic_id=heuristic.id,
            previous_heuristic="",
            new_heuristic=heuristic.heuristic,
            previous_confidence=0.0,
            new_confidence=heuristic.confidence,
            provenance_run_ids=heuristic.provenance_run_ids,
            provenance_claim_ids=heuristic.provenance_claim_ids,
            revised_by_agent=revised_by_agent,
            revised_at=now,
            rationale=proposal.rationale or "distilled from recent episodic history",
        ),
    )
    return heuristic


def revise_heuristic(
    conn: Any,
    heuristic_id: str,
    proposal: HeuristicProposal,
    *,
    revised_by_agent: str = "consolidator",
    now: datetime | None = None,
) -> AgentHeuristic | None:
    """Revise an existing heuristic append-only: update the head row and append
    a revision capturing the before/after. Returns ``None`` if the heuristic
    vanished. Provenance accumulates (union of prior + new) so the head row's
    justification only ever grows."""
    existing = get_heuristic_by_id(conn, heuristic_id)
    if existing is None:
        return None
    if not proposal.has_provenance():
        raise MissingProvenanceError(
            f"revision of {heuristic_id} has no provenance"
        )
    now = now or datetime.now(UTC)
    merged_runs = sorted(set(existing.provenance_run_ids) | set(proposal.provenance_run_ids))
    merged_claims = sorted(
        set(existing.provenance_claim_ids) | set(proposal.provenance_claim_ids)
    )
    update_heuristic(
        conn,
        heuristic_id,
        heuristic=proposal.heuristic,
        confidence=proposal.confidence,
        provenance_run_ids=merged_runs,
        provenance_claim_ids=merged_claims,
        last_revised_at=now,
        revision_count=existing.revision_count + 1,
        expires_at=now + timedelta(days=proposal.ttl_days),
    )
    create_heuristic_revision(
        conn,
        AgentHeuristicRevision(
            heuristic_id=heuristic_id,
            previous_heuristic=existing.heuristic,
            new_heuristic=proposal.heuristic,
            previous_confidence=existing.confidence,
            new_confidence=proposal.confidence,
            provenance_run_ids=proposal.provenance_run_ids,
            provenance_claim_ids=proposal.provenance_claim_ids,
            revised_by_agent=revised_by_agent,
            rationale=proposal.rationale or "re-affirmed from recent episodic history",
            revised_at=now,
        ),
    )
    return get_heuristic_by_id(conn, heuristic_id)
