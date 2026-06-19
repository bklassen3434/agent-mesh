"""Phase 2b skill: ``extract-source`` — read an unread source, emit claim effects.

This is the operational, foundational skill of the agentic market: it resolves an
``unextracted_source`` tension (a source the mesh has but no claim references yet)
by reading it and pulling structured facts out. It is a thin *wrapper* — the
extraction itself is the existing ``ClaimExtractorAgent`` and the name→id
resolution is the existing ``EntityTrackerAgent`` — so there is one extraction
implementation, not two.

The headline invariant holds: **the skill never writes.** It reads the source and
the entities through ``conn`` and returns ``CreateClaimEffect``s; the write
gateway (``mesh_db.effects.apply_effects``) is the only thing that persists them.

Entity handling is deliberately read-only. ``claims.subject_entity_id`` is a
NOT-NULL FK to ``entities(id)``, and the frozen ``Effect`` contract has no
entity-creation effect (minting an entity would be a write, and adding a new
effect kind is a separate, coordinated change). So this skill resolves each
claim's subject against entities that **already exist** in the field and emits a
claim only when the subject is known; subjects the store hasn't seen yet are
skipped (a dedicated resolution skill owns creating them). On a fresh field this
makes the skill conservative by design rather than producing claims that dangle.
"""
from __future__ import annotations

from typing import Any

from mesh_llm import LLMClient, make_llm_client
from mesh_models.claim import Claim
from mesh_models.effect import CreateClaimEffect
from mesh_models.tension import Tension, TensionKind

from mesh_agents.arxiv_scout import ScoutedPaper
from mesh_agents.claim_extractor import ClaimExtractorAgent, ClaimExtractorInput
from mesh_agents.entity_tracker import (
    EntityResolveSkillInput,
    EntitySummary,
    EntityTrackerAgent,
)
from mesh_agents.skill import Bid, register_skill


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
    """Bid on ``unextracted_source`` tensions; run the extractor and return one
    ``CreateClaimEffect`` per claim whose subject is a known entity."""

    skill_id = "extract-source"
    handles = (TensionKind.unextracted_source,)

    def __init__(self, llm: LLMClient | None = None) -> None:
        # No-arg constructable for the registry; tests inject a mock client.
        self._llm = llm

    def bid(self, conn: Any, tension: Tension) -> Bid | None:
        # Cheap, foundational work — value comes from the tension's own estimate.
        return Bid(value=tension.value, est_cost_usd=0.008)

    async def run(
        self, conn: Any, tension: Tension, *, budget_usd: float
    ) -> list[CreateClaimEffect]:
        from mesh_db.sources import get_source_by_id

        source_id = tension.target_ref.get("source_id")
        if not source_id:
            return []
        source = get_source_by_id(conn, source_id)
        if source is None:
            return []

        # 1. Extract claims via the existing agent (no DB writes; pure LLM call).
        llm = self._llm or make_llm_client()
        extractor = ClaimExtractorAgent(llm=llm)
        paper = _paper_from_source(source, tension)
        extracted = await extractor.run(ClaimExtractorInput(paper=paper))
        if not extracted.claims:
            return []

        # 2. Resolve subject names → existing entity ids via the existing tracker
        #    (pure, read-only path). New subjects resolve to is_new=True and are
        #    dropped: the skill can't mint entities without writing.
        candidate_names = list({c.subject_name for c in extracted.claims})
        existing = _load_existing_entities(conn, candidate_names, tension.field_id)
        tracker = EntityTrackerAgent()
        resolved = await tracker.run_skill(
            EntityResolveSkillInput(
                candidate_names=candidate_names,
                existing_entities=existing,
            )
        )
        name_to_id = {r.name: r.entity_id for r in resolved.resolved if not r.is_new}

        # 3. One CreateClaimEffect per claim with a known subject. The gateway is
        #    the only writer; this list is the skill's entire side effect.
        effects: list[CreateClaimEffect] = []
        for ec in extracted.claims:
            entity_id = name_to_id.get(ec.subject_name)
            if entity_id is None:
                continue
            claim = Claim(
                predicate=ec.predicate,
                subject_entity_id=entity_id,
                object=ec.object,
                source_id=source.id,
                extracted_by_agent="claim_extractor",
                raw_excerpt=ec.raw_excerpt,
                confidence=ec.confidence,
            )
            effects.append(CreateClaimEffect(field_id=tension.field_id, claim=claim))
        return effects
