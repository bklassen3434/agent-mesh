"""Phase 2b skill: ``extract-source`` — read an unread source, emit claim effects.

This is the operational, foundational skill of the agentic controller: it resolves an
``unextracted_source`` tension (a source the mesh has but no claim references yet)
by reading it and pulling structured facts out. The skill *is* the agentic unit:
it calls the shared extraction core (``extract_claims_with_memory`` — prompt + LLM
+ structured output + injected memory) directly, then the pure entity resolver. The
``ClaimExtractorAgent`` class is now only the (orphaned) A2A adapter over the same
core, so there is one extraction implementation, not two.

The headline invariant holds: **the skill never writes.** It reads the source and
the entities through ``conn`` and returns ``CreateClaimEffect``s; the write
gateway (``mesh_db.effects.apply_effects``) is the only thing that persists them.

Entity handling: ``claims.subject_entity_id`` is a NOT-NULL FK to ``entities(id)``.
A claim's subject is resolved against entities that **already exist** in the field
(exact name/alias match); a subject the store hasn't seen yet is **minted** via a
``CreateEntityEffect`` (emitted before the claim that references it, so the gateway
creates the entity first). Minting only adds — it does not dedup; near-duplicate
entities are reconciled later by the ``merge-candidate`` skill from its own
tension. A new entity is minted with its ``name_embedding`` (a local fastembed
vector, not an LLM call) so merge-candidate's similarity blocking can find it.
When no embedder is available the skill stays conservative and skips unknown
subjects rather than minting un-blockable entities.
"""
from __future__ import annotations

import asyncio
from typing import Any

from mesh_llm import (
    Embedder,
    LLMClient,
    LLMResponseError,
    make_embedder,
    make_routed_llm_client,
)
from mesh_llm.embeddings import entity_embed_text
from mesh_models.claim import Claim
from mesh_models.effect import (
    AttachClaimToInvestigationEffect,
    CreateClaimEffect,
    CreateEntityEffect,
    Effect,
    RecordExtractionAttemptEffect,
)
from mesh_models.entity import Entity, EntityType
from mesh_models.tension import Tension, TensionKind

from mesh_agents.arxiv_scout import ScoutedPaper
from mesh_agents.claim_extractor import extract_claims_with_memory
from mesh_agents.entity_tracker import (
    EntityResolveSkillInput,
    EntitySummary,
    EntityTrackerAgent,
    ResolvedEntityInfo,
)
from mesh_agents.skill import register_skill

# A source that keeps failing to parse (a transient LLM/provider hiccup) is
# retried up to this many times, then retired as exhausted rather than churning
# the tension forever. A *successful* empty extraction retires immediately (its
# emptiness is deterministic — re-reading yields the same nothing).
MAX_EXTRACTION_ATTEMPTS = 3


def _load_existing_entities(
    conn: Any, names: list[str], field_id: str
) -> list[EntitySummary]:
    """Read-only lookup of entities in ``field_id`` whose canonical name or any
    alias matches one of ``names`` (case-insensitive). Returns the lightweight
    summaries the pure resolver consumes — no rows are created."""
    if not names:
        return []
    lowered = [n.lower() for n in names]
    rows = conn.execute(
        """
        SELECT id, canonical_name, aliases, type
        FROM entities
        WHERE field_id = %s
          AND (
              lower(canonical_name) = ANY(%s)
              OR EXISTS (SELECT 1 FROM unnest(aliases) AS a WHERE lower(a) = ANY(%s))
          )
        """,
        [field_id, lowered, lowered],
    ).fetchall()
    return [
        EntitySummary(
            entity_id=str(r[0]),
            canonical_name=r[1],
            aliases=list(r[2]) if r[2] else [],
            entity_type=r[3],
        )
        for r in rows
    ]


def _paper_from_source(source: Any, tension: Tension) -> ScoutedPaper:
    """Reconstruct the ``ScoutedPaper`` the extractor expects from a stored source.

    Source *content* (title/abstract) is not persisted on the row, so we take it
    from the tension's ``signals`` when a producer enriched it there and fall back
    to empty text otherwise. The arxiv id is derived from the source url's last
    path segment (with the source id as a final fallback)."""
    arxiv_id = source.url.rstrip("/").rsplit("/", 1)[-1] or source.id
    return ScoutedPaper(
        source=source,
        title=str(tension.signals.get("title", "")),
        abstract=str(tension.signals.get("abstract", "")),
        arxiv_id=arxiv_id,
    )


@register_skill
class ExtractSourceSkill:
    """Handle ``unextracted_source`` tensions; run the extractor and return one
    ``CreateClaimEffect`` per claim whose subject is a known entity."""

    skill_id = "extract-source"
    handles = (TensionKind.unextracted_source,)

    def __init__(
        self,
        llm: LLMClient | None = None,
        embedder: Embedder | None = None,
        *,
        mint_entities: bool = True,
    ) -> None:
        # No-arg constructable for the registry; tests inject a mock client and a
        # stub embedder. ``mint_entities=False`` keeps the conservative
        # skip-unknown behaviour (used where minting is not wanted).
        self._llm = llm
        self._embedder = embedder
        self._mint_entities = mint_entities

    def _embedder_for_run(self) -> Embedder | None:
        if self._embedder is not None:
            return self._embedder
        if not self._mint_entities:
            return None
        # Lazy, like ``make_llm_client`` — the registered (no-arg) instance gets a
        # real fastembed embedder. Constructing it is cheap (the ONNX model loads
        # on first ``embed``), so a run with no new subjects never pays for it.
        return make_embedder()

    async def run(
        self, conn: Any, tension: Tension, *, budget_usd: float
    ) -> list[Effect]:
        from mesh_db.sources import get_source_by_id

        source_id = tension.target_ref.get("source_id")
        if not source_id:
            return []
        source = get_source_by_id(conn, source_id)
        if source is None:
            return []

        # 1. Extract claims via the shared extraction core (prompt + LLM +
        #    structured output + injected memory; memory reads run on this skill's
        #    connection). A parse failure yields no claims — the source is left for
        #    a later pass, matching the agent/A2A degradation. Routed as
        #    "extraction" — with routing off this is byte-for-byte the plain
        #    client, so nothing changes for unrouted deployments.
        llm = self._llm or make_routed_llm_client(agent_name="extraction")
        paper = _paper_from_source(source, tension)
        try:
            extracted_claims, _latency, _usage, _model, _debug = await asyncio.to_thread(
                extract_claims_with_memory,
                llm, paper, "claim_extractor", tension.field_id, conn,
            )
        except LLMResponseError:
            # Parse failure may be a transient provider hiccup — retry a bounded
            # number of times, then retire the source so it stops re-firing.
            attempts = getattr(source, "extraction_attempts", 0)
            return [
                RecordExtractionAttemptEffect(
                    source_id=source.id,
                    exhausted=attempts + 1 >= MAX_EXTRACTION_ATTEMPTS,
                    reason="parse_error",
                )
            ]
        if not extracted_claims:
            # A successful read that found nothing to extract (off-topic / thin
            # content). Deterministic — retire it now rather than re-reading it
            # every pass.
            return [
                RecordExtractionAttemptEffect(
                    source_id=source.id, exhausted=True, reason="no_claims"
                )
            ]

        # 2. Resolve subject names against existing entities (pure, read-only).
        # The extractor's subject_type rides along so a genuinely-new subject is
        # minted as what the source says it is, not the blanket "concept".
        candidate_names = list({c.subject_name for c in extracted_claims})
        existing = _load_existing_entities(conn, candidate_names, tension.field_id)
        type_hints: dict[str, str] = {
            c.subject_name: c.subject_type for c in extracted_claims
        }
        tracker = EntityTrackerAgent()
        resolved = await tracker.run_skill(
            EntityResolveSkillInput(
                candidate_names=candidate_names,
                existing_entities=existing,
                type_hints=type_hints,
            )
        )
        name_to_id = {r.name: r.entity_id for r in resolved.resolved if not r.is_new}

        # 3. Mint the unseen subjects (entity effects FIRST so the gateway creates
        #    each entity before the claim that FKs it). Skipped when no embedder is
        #    available — an un-blockable entity merge-candidate can never reconcile.
        effects: list[Effect] = []
        embedder = self._embedder_for_run()
        if embedder is not None:
            new_infos = [r for r in resolved.resolved if r.is_new]
            effects.extend(
                self._mint_effects(new_infos, tension.field_id, embedder, name_to_id)
            )

        # 4. One CreateClaimEffect per claim with a (now) known subject. The gateway
        #    is the only writer; this list is the skill's entire side effect. When
        #    the source was gathered for an investigation (lineage on the tension),
        #    attach each claim back so the investigation can resolve.
        investigation_id = tension.signals.get("investigation_id")
        claims_created = 0
        for ec in extracted_claims:
            entity_id = name_to_id.get(ec.subject_name)
            if entity_id is None:
                continue
            claim = Claim(
                predicate=ec.predicate,
                subject_entity_id=entity_id,
                object=ec.object.model_dump(exclude_defaults=True),
                source_id=source.id,
                extracted_by_agent="claim_extractor",
                raw_excerpt=ec.raw_excerpt,
                confidence=ec.confidence,
            )
            effects.append(CreateClaimEffect(field_id=tension.field_id, claim=claim))
            claims_created += 1
            if investigation_id:
                effects.append(
                    AttachClaimToInvestigationEffect(
                        investigation_id=str(investigation_id), claim_id=claim.id
                    )
                )

        # Extraction produced claims but none had a resolvable/mintable subject
        # (e.g. no embedder to mint against). No claim row will ever reference this
        # source, so retire it rather than leave the tension re-firing forever.
        if claims_created == 0:
            return [
                RecordExtractionAttemptEffect(
                    source_id=source.id, exhausted=True, reason="unresolved_subjects"
                )
            ]
        return effects

    @staticmethod
    def _mint_effects(
        new_infos: list[ResolvedEntityInfo],
        field_id: str,
        embedder: Embedder,
        name_to_id: dict[str, str],
    ) -> list[Effect]:
        """One ``CreateEntityEffect`` per genuinely-new subject, embedding its name
        so merge-candidate can later block on it. Registers each new id in
        ``name_to_id`` so the claim loop resolves the subject."""
        out: list[Effect] = []
        for info in new_infos:
            etype = EntityType(info.entity_type)
            entity = Entity(
                id=info.entity_id, canonical_name=info.canonical_name, type=etype
            )
            try:
                vec: list[float] | None = embedder.embed(
                    [entity_embed_text(entity.canonical_name, etype.value)]
                )[0]
            except Exception:  # embedding is best-effort; mint without it
                vec = None
            out.append(
                CreateEntityEffect(field_id=field_id, entity=entity, name_embedding=vec)
            )
            name_to_id[info.name] = info.entity_id
        return out
