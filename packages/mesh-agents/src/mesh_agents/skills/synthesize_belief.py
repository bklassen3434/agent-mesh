"""Phase 2 skill: ``synthesize-belief`` — claims → beliefs (and graph edges).

Resolves an ``unsynthesized_claims`` tension: for the entity it names, turn the
entity's active claims into knowledge by **reusing the existing synthesizers**
unchanged —

* ``score`` claims  → ``sota_tracker.update_sota_pure`` (leaderboard beliefs),
* ``capability`` claims → ``synthesis.synthesize_capability_belief`` (entity-anchored
  ``capability:<entity_id>`` beliefs),
* relational claims → ``synthesis.edge_for_claim`` (claim-grounded graph edges),

and translate each synthesizer output into an ``Effect``:

* a new belief    → ``CreateBeliefEffect``,
* a changed belief → ``ReviseBeliefEffect`` (``revised_by_agent="synthesize-belief"``),
* a relationship  → ``AddRelationshipEvidenceEffect``.

The skill **never writes** — it only reads the board and returns intents; the
write gateway (``mesh_db.effects.apply_effects``) applies them under the store's
invariants. ``tension.field_id`` is threaded through every read and every effect,
so synthesis never crosses fields.

Confidence here is the synthesizer's seed/prior value, not the evidence-derived
score the coordinator recomputes post-write (that read-after-write recompute is a
gateway concern; a skill that emits intents can't see its own writes). The
confidence converges once the effect lands and a later signal pass runs.
"""
from __future__ import annotations

from typing import Any

from mesh_db.beliefs import list_beliefs
from mesh_db.claims import list_claims
from mesh_db.entities import get_entity_by_id, list_entities
from mesh_llm import Embedder, make_embedder
from mesh_llm.embeddings import entity_embed_text
from mesh_models.belief import Belief
from mesh_models.claim import Claim, ClaimStatus, ClaimType
from mesh_models.effect import (
    AddRelationshipEvidenceEffect,
    CreateBeliefEffect,
    CreateEntityEffect,
    Effect,
    MarkClaimsSynthesizedEffect,
    ReviseBeliefEffect,
)
from mesh_models.entity import Entity, EntityType
from mesh_models.tension import Tension, TensionKind

from mesh_agents.skill import register_skill
from mesh_agents.sota_tracker import (
    BeliefSummary,
    BeliefUpdate,
    ResolvedClaim,
    update_sota_pure,
)
from mesh_agents.synthesis import (
    CAPABILITY_TOPIC_PREFIX,
    CapabilityBeliefInput,
    ExistingCapabilityBelief,
    capability_topic,
    edge_for_claim,
    synthesize_capability_belief,
)

# Attribution stamped onto every revision this skill produces (the gateway copies
# it onto the append-only BeliefRevision row).
REVISED_BY = "synthesize-belief"

# Per-entity cap on active claims scanned per run — bounds the read; synthesis is
# idempotent so anything past the cap is picked up on a later round.
_MAX_CLAIMS = 500

# Claim types synthesis never turns into anything (they feed the skeptic /
# contradiction path). Mirrors the exclusion in
# ``unsynthesized_claim_counts_by_entity`` so the marker set matches the count.
_NON_SYNTHESIZABLE = frozenset(
    {ClaimType.critique, ClaimType.reproduction, ClaimType.speculative}
)


def _to_resolved(claim: Claim) -> ResolvedClaim:
    """The synthesizers speak ``ResolvedClaim``; the entity is already resolved on
    a stored claim, so this is a pure shape adapter."""
    return ResolvedClaim(
        claim_id=claim.id,
        subject_entity_id=claim.subject_entity_id,
        predicate=claim.predicate,
        claim_type=claim.claim_type,
        object=claim.object,
        source_id=claim.source_id,
        raw_excerpt=claim.raw_excerpt,
        confidence=claim.confidence,
    )


def _belief_update_to_effect(update: BeliefUpdate, field_id: str) -> Effect:
    """A synthesizer ``BeliefUpdate`` is either a new belief or a revision of an
    existing one — map each to its effect (decision → intent, no write)."""
    if update.is_new_belief:
        return CreateBeliefEffect(
            field_id=field_id,
            belief=Belief(
                topic=update.topic,
                statement=update.new_statement,
                supporting_claim_ids=update.supporting_claim_ids,
                confidence=update.new_confidence,
            ),
        )
    assert update.existing_belief_id is not None
    return ReviseBeliefEffect(
        belief_id=update.existing_belief_id,
        new_statement=update.new_statement,
        new_confidence=update.new_confidence,
        revised_by_agent=REVISED_BY,
        rationale=update.rationale,
        trigger_claim_ids=update.supporting_claim_ids,
        supporting_claim_ids=update.supporting_claim_ids,
    )


def _score_effects(
    conn: Any, claims: list[Claim], *, field_id: str
) -> list[Effect]:
    """Leaderboard beliefs from the entity's ``score`` claims, compared against the
    field's existing SOTA beliefs (``sota_tracker.update_sota_pure``)."""
    score_claims = [c for c in claims if c.claim_type == ClaimType.score]
    if not score_claims:
        return []
    existing_sota = [
        BeliefSummary(
            belief_id=b.id, topic=b.topic, statement=b.statement, confidence=b.confidence
        )
        for b in list_beliefs(conn, currently_held=True, limit=1000, field_id=field_id)
        if b.topic.startswith("sota:")
    ]
    updates = update_sota_pure([_to_resolved(c) for c in score_claims], existing_sota)
    return [_belief_update_to_effect(u, field_id) for u in updates]


def _capability_effects(
    conn: Any, entity_id: str, claims: list[Claim], *, field_id: str
) -> list[Effect]:
    """The entity-anchored ``capability:<entity_id>`` belief, rebuilt from the
    entity's full active capability claim set (``synthesize_capability_belief``)."""
    cap_claims = [c for c in claims if c.claim_type == ClaimType.capability]
    if not cap_claims:
        return []
    ent = get_entity_by_id(conn, entity_id)
    name = ent.canonical_name if ent is not None else entity_id
    existing_b = next(
        (
            b
            for b in list_beliefs(
                conn, currently_held=True, limit=1000, field_id=field_id
            )
            if b.topic == capability_topic(entity_id)
            and b.topic.startswith(CAPABILITY_TOPIC_PREFIX)
        ),
        None,
    )
    eb = (
        ExistingCapabilityBelief(
            belief_id=existing_b.id,
            statement=existing_b.statement,
            confidence=existing_b.confidence,
            supporting_claim_ids=existing_b.supporting_claim_ids,
        )
        if existing_b is not None
        else None
    )
    update = synthesize_capability_belief(
        CapabilityBeliefInput(
            entity_id=entity_id,
            entity_name=name,
            claims=[_to_resolved(c) for c in cap_claims],
            existing_belief=eb,
        )
    )
    if update is None:  # nothing to assert, or already in sync (idempotent re-run)
        return []
    return [_belief_update_to_effect(update, field_id)]


def _resolve_entity_by_name(conn: Any, name: str, *, field_id: str) -> str | None:
    """Resolve a relational claim's *target* entity name to an id within the field.

    Exact (case-insensitive) match on canonical name or alias; the substring
    ``q`` is just a prefilter. Returns ``None`` when the target isn't a known
    entity — the edge is skipped, never fabricated against a missing node."""
    key = name.strip().lower()
    if not key:
        return None
    for e in list_entities(conn, q=name, field_id=field_id, limit=50):
        if e.canonical_name.strip().lower() == key or any(
            a.strip().lower() == key for a in e.aliases
        ):
            return e.id
    return None


# What kind of node a relational edge points at, so a minted target is typed as
# what it is (a benchmark, a lab, another model) rather than the blanket
# "concept" — keyed by the edge type ``edge_for_claim`` returns.
_EDGE_TARGET_TYPE: dict[str, EntityType] = {
    "evaluated_on": EntityType.benchmark,  # evaluation → the benchmark
    "developed_by": EntityType.lab,        # attribution → the lab
    "outperforms": EntityType.model,       # comparison → the compared-to model
    "based_on": EntityType.model,          # lineage → the parent it builds on
}


def _edge_effects(
    conn: Any,
    entity_id: str,
    claims: list[Claim],
    *,
    field_id: str,
    embedder: Embedder | None = None,
) -> list[Effect]:
    """Claim-grounded relationship edges from the entity's relational claims
    (``edge_for_claim``). Self-filters: non-relational claims map to ``None``.

    When the edge's target names an entity the field doesn't have yet, the target
    is **minted** (typed by edge kind, with a name embedding so merge-candidate can
    later block on it) rather than skipped — the old skip left the claim forever
    unhandled, so its ``unsynthesized_claims`` tension re-fired every pass (the
    dominant source of synthesize-belief churn) and the benchmark/lab nodes never
    entered the graph. Mint effects come first so the gateway creates each target
    before the edge that FKs it. Self-loops are still skipped. With no embedder we
    fall back to the conservative skip (an un-blockable node can never reconcile)."""
    mint_effects: list[Effect] = []
    edge_effects: list[Effect] = []
    minted: dict[str, str] = {}  # lower(name) -> new entity id (dedup within run)
    for c in claims:
        spec = edge_for_claim(c.claim_type, c.object)
        if spec is None:
            continue
        edge_type, target_name = spec
        target_id = _resolve_entity_by_name(conn, target_name, field_id=field_id)
        if target_id is None:
            key = target_name.strip().lower()
            if key in minted:
                target_id = minted[key]
            elif embedder is not None:
                target_id = _mint_target(
                    target_name, edge_type, field_id, embedder, mint_effects
                )
                minted[key] = target_id
            else:
                continue  # no embedder → can't mint a blockable node; skip
        if target_id == entity_id:
            continue
        edge_effects.append(
            AddRelationshipEvidenceEffect(
                field_id=field_id,
                from_entity_id=entity_id,
                to_entity_id=target_id,
                type=edge_type,
                claim_id=c.id,
                confidence=c.confidence,
            )
        )
    return mint_effects + edge_effects


def _mint_target(
    target_name: str,
    edge_type: str,
    field_id: str,
    embedder: Embedder,
    out: list[Effect],
) -> str:
    """Append a ``CreateEntityEffect`` for a not-yet-known edge target and return
    its id. Typed by edge kind; embedded (best-effort) so merge-candidate can
    block on it — mirrors extract-source's subject minting."""
    etype = _EDGE_TARGET_TYPE.get(edge_type, EntityType.concept)
    entity = Entity(canonical_name=target_name.strip(), type=etype)
    try:
        vec: list[float] | None = embedder.embed(
            [entity_embed_text(entity.canonical_name, etype.value)]
        )[0]
    except Exception:  # embedding is best-effort; mint without it
        vec = None
    out.append(CreateEntityEffect(field_id=field_id, entity=entity, name_embedding=vec))
    return entity.id


@register_skill
class SynthesizeBeliefSkill:
    """Turns an entity's unsynthesized claims into beliefs and graph edges by
    reusing the existing synthesizers, emitting effects (never writing)."""

    skill_id = "synthesize-belief"
    handles = (TensionKind.unsynthesized_claims,)
    # LLM-free: keeps running while the daily LLM budget brake is engaged. Minting
    # an edge target embeds its name with a LOCAL fastembed model (no LLM call), so
    # this stays true.
    uses_llm = False

    def __init__(self, embedder: Embedder | None = None) -> None:
        # No-arg constructable for the registry; tests inject a stub embedder.
        self._embedder = embedder

    def _embedder_for_run(self) -> Embedder | None:
        if self._embedder is not None:
            return self._embedder
        # Lazy, like extract-source: the ONNX model loads on first ``embed``, so a
        # run whose targets all resolve never pays for it.
        return make_embedder()

    async def run(
        self, conn: Any, tension: Tension, *, budget_usd: float
    ) -> list[Effect]:
        entity_id = tension.target_ref.get("entity_id")
        if not entity_id:
            return []
        field_id = tension.field_id

        # One read of the entity's active claims, partitioned by the handlers below
        # (mirrors the coordinator's score / capability / edge split).
        claims = list_claims(
            conn,
            entity_id=entity_id,
            status=ClaimStatus.active,
            limit=_MAX_CLAIMS,
            field_id=field_id,
        )

        effects: list[Effect] = []
        effects.extend(_score_effects(conn, claims, field_id=field_id))
        effects.extend(
            _capability_effects(conn, entity_id, claims, field_id=field_id)
        )
        effects.extend(
            _edge_effects(
                conn,
                entity_id,
                claims,
                field_id=field_id,
                embedder=self._embedder_for_run(),
            )
        )

        # Terminal state: mark every synthesizable claim we just processed, so a
        # claim that synthesis considered but left un-membered (a non-leader score,
        # an already-covered capability) stops re-firing this entity's tension. A
        # genuinely new claim arrives unmarked and re-triggers a full re-read. The
        # marker set mirrors the count's synthesizable filter exactly.
        processed = [c.id for c in claims if c.claim_type not in _NON_SYNTHESIZABLE]
        if processed:
            effects.append(MarkClaimsSynthesizedEffect(claim_ids=processed))
        return effects
