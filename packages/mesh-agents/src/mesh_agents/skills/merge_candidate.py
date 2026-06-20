"""Phase 2 skill: ``merge-candidate`` — decide if two entities are one thing.

Wraps the existing semantic entity-resolution adjudicator (``mesh_agents.
entity_resolution``) into the agentic skill contract. The blocking step already
happened upstream: a ``merge_candidate`` tension carries a primary entity, a
look-alike candidate, and their cosine similarity. This skill turns that score
into a decision — auto-merge when very confident, auto-reject when clearly
unrelated, LLM-adjudicate the uncertain middle band — and, when the verdict is
"same entity", emits a single :class:`MergeEntitiesEffect`.

The decision/write split holds: the skill **never** calls ``merge_entities``
directly. It only returns the effect; the write gateway
(``mesh_db.effects.apply_effects``) performs the transactional re-point. Which id
is canonical vs duplicate is decided by the same deterministic ``choose_canonical``
rule the reconcile pass uses. Conservative throughout — an unparseable LLM reply,
a missing entity, or no adjudication LLM all resolve to "don't merge" (a missed
merge is cheap; a false merge corrupts provenance).
"""
from __future__ import annotations

from typing import Any

from mesh_db.claims import list_claims
from mesh_db.connection import MeshConnection
from mesh_db.entities import choose_canonical, get_entity_by_id
from mesh_llm.protocol import LLMClient
from mesh_models.effect import MergeEntitiesEffect
from mesh_models.tension import Tension, TensionKind

from mesh_agents.entity_resolution import (
    ResolutionConfig,
    adjudicate_same_entity,
    classify_pair,
    entity_for_match_from_claims,
)
from mesh_agents.skill import register_skill

# Flat per-decision cost estimate (matches the agenda's ``_KIND_COST_USD`` entry
# for ``merge_candidate``): most pairs resolve via the cheap high/low bands, only
# the middle band spends one small adjudication call.
_EST_COST_USD = 0.02


@register_skill
class MergeCandidateSkill:
    """Adjudicate one entity-duplicate pair → at most one ``MergeEntitiesEffect``."""

    skill_id = "merge-candidate"
    handles = (TensionKind.merge_candidate,)

    def __init__(self, llm: LLMClient | None = None) -> None:
        # Injectable for tests; in production the adjudication client is built
        # lazily in ``run`` only when the middle band actually needs it.
        self._llm = llm

    async def run(
        self, conn: MeshConnection, tension: Tension, *, budget_usd: float
    ) -> list[Any]:
        entity_id = tension.target_ref.get("entity_id")
        candidate_id = tension.signals.get("candidate_id") or tension.target_ref.get(
            "candidate_id"
        )
        if not entity_id or not candidate_id:
            return []

        try:
            similarity = float(tension.signals.get("similarity", 0.0))
        except (TypeError, ValueError):
            return []

        cfg = ResolutionConfig.from_env()
        decision = classify_pair(similarity, cfg)
        if decision == "reject":
            return []

        ent_a = get_entity_by_id(conn, entity_id)
        ent_b = get_entity_by_id(conn, candidate_id)
        if ent_a is None or ent_b is None:
            return []

        if decision == "adjudicate":
            llm = self._resolve_llm()
            if llm is None:
                # No adjudicator → leave the duplicate (conservative).
                return []
            a = entity_for_match_from_claims(
                ent_a.canonical_name,
                ent_a.type.value,
                aliases=list(ent_a.aliases),
                claims=list_claims(
                    conn, entity_id=entity_id, limit=3, field_id=tension.field_id
                ),
            )
            b = entity_for_match_from_claims(
                ent_b.canonical_name,
                ent_b.type.value,
                aliases=list(ent_b.aliases),
                claims=list_claims(
                    conn, entity_id=candidate_id, limit=3, field_id=tension.field_id
                ),
            )
            if not adjudicate_same_entity(llm, a, b).same_entity:
                return []
        # else: decision == "merge" — high band, no LLM needed.

        canonical_id, duplicate_id = choose_canonical(conn, entity_id, candidate_id)
        return [
            MergeEntitiesEffect(canonical_id=canonical_id, duplicate_id=duplicate_id)
        ]

    def _resolve_llm(self) -> LLMClient | None:
        """The injected client if present, else a best-effort routed adjudication
        client. A missing/unready provider degrades to "no merge" rather than
        aborting — same posture as the coordinator's resolution deps."""
        if self._llm is not None:
            return self._llm
        try:
            from mesh_llm import make_routed_llm_client

            llm = make_routed_llm_client(agent_name="entity_resolution")
            llm.health_check()
            return llm
        except Exception:
            return None
