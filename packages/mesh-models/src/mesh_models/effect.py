"""Phase 1 of the agentic migration: Effects — typed write intents.

The headline invariant of the agentic redesign: **a skill decides, it never
writes.** A skill returns a list of ``Effect``s — declarative descriptions of the
mutations it wants — and a single deterministic write gateway
(``mesh_db.effects.apply_effects``) applies them under the store's invariants
(claims immutable, belief revisions append-only, coordinator-owned writes,
attribution). The LLM reasoning that *chose* an effect lives above this boundary
and cannot route around it.

This module is the frozen contract: the shapes here, plus the gateway that
consumes them, are what every Phase-2 skill worktree builds against. Effects are
a discriminated union (on ``kind``) so they survive the JSON round-trip through
LangGraph checkpoint state.

Granularity mirrors the coordinator's existing write operations, so the gateway
can eventually replace those call sites one-for-one (strangler-fig): the old
coordinator keeps writing directly while skills accumulate behind the gateway.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.entity import Entity
from mesh_models.heuristic import AgentHeuristic, AgentHeuristicRevision
from mesh_models.investigation import Investigation
from mesh_models.source import Source


class CreateSourceEffect(BaseModel):
    kind: Literal["create_source"] = "create_source"
    field_id: str
    source: Source


class CreateEntityEffect(BaseModel):
    """Mint a new entity. Emitted by ``extract-source`` for a claim subject that
    resolves to no existing entity in the field, so a fresh field can bootstrap
    (the frozen Phase-1 contract had no entity-creation effect, which left the
    extractor unable to create claims on an empty field). Dedup is *not* this
    effect's job — the ``merge-candidate`` skill reconciles near-duplicates from
    its own tension; this only adds. ``name_embedding`` (a local fastembed vector,
    not an LLM call) is populated by the gateway so entity-resolution blocking can
    find the new entity later."""

    kind: Literal["create_entity"] = "create_entity"
    field_id: str
    entity: Entity
    name_embedding: list[float] | None = None


class CreateClaimEffect(BaseModel):
    """Insert a new immutable claim. Claims are never updated — new evidence is a
    new claim (+ optional SupersedeClaimEffect on the old one)."""

    kind: Literal["create_claim"] = "create_claim"
    field_id: str
    claim: Claim


class SupersedeClaimEffect(BaseModel):
    """Mark a claim superseded (the only mutation claims allow). Optionally point
    at the claim that replaced it."""

    kind: Literal["supersede_claim"] = "supersede_claim"
    claim_id: str
    superseded_by_claim_id: str | None = None


class CreateBeliefEffect(BaseModel):
    kind: Literal["create_belief"] = "create_belief"
    field_id: str
    belief: Belief
    # Local fastembed vector of (topic, statement) so the Phase-19 belief
    # consolidation sweep can block on it; the gateway persists it (no LLM).
    statement_embedding: list[float] | None = None


class ReviseBeliefEffect(BaseModel):
    """Append a revision to a held belief (never an in-place overwrite). The
    gateway reads the current belief to fill ``previous_*``, writes a revision
    row, then updates the head — so the append-only history is enforced in one
    place, not in each skill. ``supporting/contradicting_claim_ids`` are optional
    full-set replacements when the evidence set changed."""

    kind: Literal["revise_belief"] = "revise_belief"
    belief_id: str
    new_statement: str
    new_confidence: float = Field(ge=0.0, le=1.0)
    revised_by_agent: str
    rationale: str
    trigger_claim_ids: list[str] = Field(default_factory=list)
    supporting_claim_ids: list[str] | None = None
    contradicting_claim_ids: list[str] | None = None
    # Re-embed the head when the statement changed (gateway persists it; no LLM).
    new_statement_embedding: list[float] | None = None
    # Drop the belief out of the held set (staleness archival). Append-only — the
    # row and its revisions stay; only ``is_currently_held`` flips false.
    set_not_held: bool = False
    # When False, the gateway uses ``new_confidence`` verbatim instead of the
    # injected evidence-derived ``confidence_fn``. Maintenance revisions (decay /
    # archival) set their own confidence by design and must not be re-derived from
    # evidence signals; synthesis revisions keep the default (recompute).
    recompute_confidence: bool = True


class MergeEntitiesEffect(BaseModel):
    """Merge a duplicate entity into a canonical one (transactional re-pointing in
    ``mesh_db.entities.merge_entities``). Never touches claim content."""

    kind: Literal["merge_entities"] = "merge_entities"
    canonical_id: str
    duplicate_id: str


class WriteFieldBriefEffect(BaseModel):
    """Persist one LLM-written "state of the field" narrative (append-only).

    Emitted by the ``write-field-brief`` skill; the gateway inserts a
    ``field_briefs`` row. Readers (the Field Overview API) take the latest."""

    kind: Literal["write_field_brief"] = "write_field_brief"
    field_id: str
    narrative: str
    model: str = ""
    inputs_summary: dict[str, Any] = Field(default_factory=dict)


class RejectEntityMergeEffect(BaseModel):
    """Record that an adjudicated entity pair is NOT the same thing.

    The durable complement of :class:`MergeEntitiesEffect`: a "no merge"
    verdict used to be returned as no effect at all, so the duplicate-pair
    scan re-derived the same pair every sensing pass and the same LLM
    adjudication re-ran forever. The gateway writes one idempotent
    ``entity_merge_rejections`` row; the scan skips rejected pairs. IDs are
    normalized so ``entity_id_a < entity_id_b``."""

    kind: Literal["reject_entity_merge"] = "reject_entity_merge"
    entity_id_a: str
    entity_id_b: str
    field_id: str
    similarity: float | None = None


class MergeBeliefsEffect(BaseModel):
    """Fold a redundant belief into a canonical one (append-only ``merge_beliefs``).

    The belief analog of :class:`MergeEntitiesEffect`, emitted by the
    ``consolidate-beliefs`` skill when two held beliefs in the same family embed
    near-identically. Strictly append-only: the duplicate is marked
    ``is_currently_held = false`` and absorbed (its claim-id unions fold onto the
    canonical) — no belief or revision row is ever deleted, and claim content is
    never touched. The gateway recomputes the canonical's confidence from the
    enlarged evidence via the injected ``confidence_fn``."""

    kind: Literal["merge_beliefs"] = "merge_beliefs"
    canonical_id: str
    duplicate_id: str


class AddRelationshipEvidenceEffect(BaseModel):
    """Claim-grounded edge upsert (one edge per from/to/type, evidence aggregated)."""

    kind: Literal["add_relationship_evidence"] = "add_relationship_evidence"
    field_id: str
    from_entity_id: str
    to_entity_id: str
    type: str
    claim_id: str
    confidence: float = Field(ge=0.0, le=1.0)


class OpenInvestigationEffect(BaseModel):
    kind: Literal["open_investigation"] = "open_investigation"
    field_id: str
    investigation: Investigation


class UpdateInvestigationEffect(BaseModel):
    """Advance an investigation's lifecycle (the only mutation it allows here):
    move it to in_progress while a dispatch gathers evidence, or resolve/abandon
    it. ``increment_attempts`` bumps ``pipeline_runs_attempted`` from the live
    row; ``set_resolved_at`` stamps the resolution time."""

    kind: Literal["update_investigation"] = "update_investigation"
    investigation_id: str
    status: str | None = None  # an InvestigationStatus value
    increment_attempts: bool = False
    set_resolved_at: bool = False


class AttachClaimToInvestigationEffect(BaseModel):
    """Link a claim gathered for an investigation to it (append-only on
    ``collected_claim_ids``). Emitted by extract-source when the source it read was
    acquired for an investigation (carried in the source payload's lineage)."""

    kind: Literal["attach_claim_to_investigation"] = "attach_claim_to_investigation"
    investigation_id: str
    claim_id: str


class WriteHeuristicEffect(BaseModel):
    """Persist a newly-distilled procedural heuristic + its genesis revision
    (append-only). Emitted by the ``consolidate-memory`` skill once it has
    distilled an agent's recent episodic history into a candidate heuristic and
    bound it to provenance. The skill builds both rows (it owns the consolidation
    logic); the gateway only inserts them, so the coordinator-owned-write boundary
    holds. Like the belief/entity writes, nothing is ever deleted — a stale
    heuristic simply expires (``expires_at``) and is filtered at read time."""

    kind: Literal["write_heuristic"] = "write_heuristic"
    field_id: str
    heuristic: AgentHeuristic
    genesis_revision: AgentHeuristicRevision


# Discriminated union — match on ``.kind`` in the gateway; JSON-safe for
# checkpoint state. Extend here (and add a branch to apply_effects) when a new
# kind of write is needed; that is the one coordination point across skill
# worktrees, so keep it small and append-only.
Effect = Annotated[
    CreateSourceEffect
    | CreateEntityEffect
    | CreateClaimEffect
    | SupersedeClaimEffect
    | CreateBeliefEffect
    | ReviseBeliefEffect
    | MergeEntitiesEffect
    | RejectEntityMergeEffect
    | MergeBeliefsEffect
    | AddRelationshipEvidenceEffect
    | OpenInvestigationEffect
    | UpdateInvestigationEffect
    | AttachClaimToInvestigationEffect
    | WriteHeuristicEffect
    | WriteFieldBriefEffect,
    Field(discriminator="kind"),
]
