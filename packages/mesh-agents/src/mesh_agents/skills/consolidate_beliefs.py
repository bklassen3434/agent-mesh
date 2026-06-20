"""Skill: ``consolidate-beliefs`` — decide if two held beliefs say the same thing.

The belief analog of ``merge-candidate``. The blocking step already happened
upstream: a ``redundant_beliefs`` tension carries two held, same-family beliefs
and their cosine similarity. This skill turns that score into a decision —
auto-merge when very confident, auto-reject when clearly distinct, LLM-adjudicate
the uncertain middle band — and, when the verdict is "same proposition", emits a
single :class:`MergeBeliefsEffect`.

This is what makes "if beliefs are very similar, consolidate them" a first-class
controller capability rather than a separately-scheduled job: the controller's
consolidation rule routes the tension here, the skill decides, and the write
gateway performs the strictly append-only ``merge_beliefs`` (the duplicate is
absorbed and marked not-held — no row is ever deleted, no claim is touched).

The decision/write split holds: the skill **never** calls ``merge_beliefs``
directly. Conservative throughout — an unparseable LLM reply, a missing belief,
or no adjudication LLM all resolve to "don't merge" (a missed consolidation is
caught next round; a false merge buries a distinct belief).
"""
from __future__ import annotations

from typing import Any

from mesh_db.beliefs import choose_canonical_belief, get_belief_by_id
from mesh_llm.protocol import LLMClient
from mesh_models.effect import MergeBeliefsEffect
from mesh_models.tension import Tension, TensionKind

from mesh_agents.belief_consolidation import (
    BeliefMergeConfig,
    adjudicate_beliefs,
    band,
    belief_for_match,
)
from mesh_agents.skill import register_skill

# Flat per-decision cost estimate (matches the agenda's ``_KIND_COST_USD`` entry
# for ``redundant_beliefs``): most pairs resolve via the cheap high/low bands,
# only the middle band spends one small adjudication call.
_EST_COST_USD = 0.02


@register_skill
class ConsolidateBeliefsSkill:
    """Adjudicate one redundant-belief pair → at most one ``MergeBeliefsEffect``."""

    skill_id = "consolidate-beliefs"
    handles = (TensionKind.redundant_beliefs,)

    def __init__(self, llm: LLMClient | None = None) -> None:
        # Injectable for tests; in production the adjudication client is built
        # lazily in ``run`` only when the middle band actually needs it.
        self._llm = llm

    async def run(
        self, conn: Any, tension: Tension, *, budget_usd: float
    ) -> list[Any]:
        belief_id = tension.target_ref.get("belief_id")
        candidate_id = tension.signals.get("candidate_id") or tension.target_ref.get(
            "candidate_id"
        )
        if not belief_id or not candidate_id:
            return []

        try:
            similarity = float(tension.signals.get("similarity", 0.0))
        except (TypeError, ValueError):
            return []

        cfg = BeliefMergeConfig.from_env()
        decision = band(similarity, cfg)
        if decision == "reject":
            return []

        belief_a = get_belief_by_id(conn, belief_id)
        belief_b = get_belief_by_id(conn, candidate_id)
        if belief_a is None or belief_b is None:
            return []
        if not belief_a.is_currently_held or not belief_b.is_currently_held:
            return []  # one was already absorbed — nothing to do

        if decision == "adjudicate":
            llm = self._resolve_llm()
            if llm is None:
                return []  # no adjudicator → leave the pair (conservative)
            if not adjudicate_beliefs(
                llm, belief_for_match(belief_a), belief_for_match(belief_b)
            ):
                return []
        # else: decision == "merge" — high band, no LLM needed.

        canonical_id, duplicate_id = choose_canonical_belief(
            conn, belief_id, candidate_id
        )
        return [
            MergeBeliefsEffect(canonical_id=canonical_id, duplicate_id=duplicate_id)
        ]

    def _resolve_llm(self) -> LLMClient | None:
        """The injected client if present, else a best-effort routed adjudication
        client. A missing/unready provider degrades to "no merge" rather than
        aborting — same posture as the Phase-19 consolidation sweep."""
        if self._llm is not None:
            return self._llm
        try:
            from mesh_llm import make_routed_llm_client

            llm = make_routed_llm_client(agent_name="belief_consolidator")
            llm.health_check()
            return llm
        except Exception:
            return None
