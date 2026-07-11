"""Skill: ``maintain-belief`` — age the held belief corpus (LLM-free).

The controller analog of the Phase-19 sweep's second pass. A cooldown-gated
``aging_belief`` tension (one per field, due on a timer like scouting) routes
here; the skill plans the deterministic decay/archival actions over the held
corpus and emits one append-only ``ReviseBeliefEffect`` per action — confidence
decays toward the floor for stale beliefs, and long-dead unsupported beliefs flip
out of the held set. No LLM, no row ever deleted, no claim touched.

This is what folds belief decay/archival into the blackboard: the periodic batch
job is gone, and "age the corpus on a timer" is now a rule + a skill + effects,
applied through the same gateway as every other write. The decision/write split
holds — the skill never updates a belief directly; ``plan_decay_and_archive`` is
read-only and the gateway performs the revision.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mesh_models.effect import ReviseBeliefEffect
from mesh_models.tension import Tension, TensionKind

from mesh_agents.belief_reconcile import plan_decay_and_archive
from mesh_agents.skill import register_skill

_AGENT = "belief_consolidator"
# LLM-free: the only cost is a corpus scan. Kept tiny so the agenda/cost views
# don't over-weight a maintenance pass.
_EST_COST_USD = 0.001


@register_skill
class MaintainBeliefSkill:
    """Plan the field's decay/archival actions → append-only ``ReviseBeliefEffect``s."""

    skill_id = "maintain-belief"
    handles = (TensionKind.aging_belief,)
    # LLM-free: keeps running while the daily LLM budget brake is engaged.
    uses_llm = False

    async def run(
        self, conn: Any, tension: Tension, *, budget_usd: float
    ) -> list[Any]:
        field_id = tension.target_ref.get("field_id") or tension.field_id
        decisions = plan_decay_and_archive(
            conn, now=datetime.now(UTC), field_id=field_id
        )
        return [
            ReviseBeliefEffect(
                belief_id=d.belief_id,
                new_statement=d.statement,  # unchanged — aging never rewrites it
                new_confidence=d.new_confidence,
                revised_by_agent=_AGENT,
                rationale=d.rationale,
                set_not_held=d.archive,
                # Decay/archival set confidence deliberately; don't let the
                # gateway re-derive it from evidence signals.
                recompute_confidence=False,
            )
            for d in decisions
        ]
