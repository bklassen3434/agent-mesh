"""Deep skill: ``adjudicate-contradiction`` — weigh a contradicted load-bearing belief.

The flagship *deep* reasoning case. Where ``challenge-belief`` is a single
swarm-tier skeptic pass, this skill runs a **plan → gather → reason → decide loop
that unfolds across controller rounds** — it never loops in-process; it advances a
small state machine on the board and lets the controller's re-sense be the loop:

1. **Plan / gather** — first dispatch (no adjudication investigation yet): open one
   ``origin=adjudication`` investigation that asks "does corroborating evidence
   support or refute <belief>?". The normal open-investigation → dispatch →
   extract chain gathers and attaches evidence over the next rounds. While that
   investigation is in flight the originating ``contradicted_belief`` tension is
   *suppressed* by its producer, so this skill is not re-dispatched (and guards
   for it anyway).
2. **Reason / decide** — once that investigation terminates (resolved/abandoned),
   the tension re-surfaces. The skill weighs the belief's own evidence plus the
   gathered corroboration against the fresh contradicting claims (the shared
   skeptic core) and emits exactly one ``ReviseBeliefEffect``
   (``revised_by_agent="adjudicator"``) recording the verdict — confidence down on
   a refutation (dropped from the held set if it collapses), unchanged on a
   survival. Either way the revision **cites the fresh contradicting claim ids**,
   which is what marks them adjudicated and stops the producer re-firing — the
   loop's termination guarantee.

Like every skill it **never writes** — it returns effects for the gateway — and is
**idempotent per dispatch**: it reads board state to decide which step it is on.
Adjudication is deliberately revision-only (it never supersedes a claim): claims
are immutable evidence, so a verdict adjusts the belief, not the record.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from mesh_db.investigations import list_investigations
from mesh_llm import make_routed_llm_client
from mesh_models.effect import Effect, OpenInvestigationEffect, ReviseBeliefEffect
from mesh_models.investigation import (
    Investigation,
    InvestigationOrigin,
    InvestigationStatus,
)
from mesh_models.tension import Tension, TensionKind

from mesh_agents.skeptic import SkepticAssessment, SkepticInput, challenge_belief_with_memory
from mesh_agents.skill import register_skill
from mesh_agents.skills.challenge_belief import (
    _collect_in_scope_entities,
    _hydrate_claims,
)
from mesh_agents.sota_tracker import BeliefSummary

logger = logging.getLogger(__name__)

# Gather (open investigation) + one weigh call once it terminates.
_EST_COST_USD = 0.08

_ADJUDICATOR_AGENT = "adjudicator"

_TERMINAL = (InvestigationStatus.resolved, InvestigationStatus.abandoned)


def _refute_floor() -> float:
    """Confidence below which a *contradicted* verdict drops the belief out of the
    held set (append-only — the row and its revisions stay, ``is_currently_held``
    flips false)."""
    import os

    return float(os.environ.get("MESH_ADJUDICATE_REFUTE_FLOOR", "0.2"))


def _adjudication_investigations(conn: Any, field_id: str, belief_id: str) -> list[Investigation]:
    """Every adjudication sub-investigation this belief has spawned, newest activity
    last is not guaranteed — callers only care whether any is non-terminal."""
    return [
        inv
        for inv in list_investigations(
            conn, origin=InvestigationOrigin.adjudication, field_id=field_id, limit=200
        )
        if inv.opened_by_belief_id == belief_id
    ]


def _open_gather(belief: Any, field_id: str, allowed_source_types: list[str]) -> Investigation:
    """The plan step: one investigation that gathers corroboration for/against the
    belief before it is weighed."""
    return Investigation(
        question=f"Does corroborating evidence support or refute: {belief.statement}",
        hypothesis=belief.statement,
        suggested_source_types=allowed_source_types,
        opened_by_belief_id=belief.id,
        origin=InvestigationOrigin.adjudication,
        trigger_rationale=(
            f"Load-bearing belief contradicted by fresh evidence "
            f"(confidence {belief.confidence:.2f}); gathering corroboration to adjudicate."
        ),
        priority=0.9,
    )


def _decide_effects(
    belief: Any,
    assessment: SkepticAssessment,
    fresh_contradicting_ids: list[str],
    now: datetime,
) -> list[Effect]:
    """The decide step: one adjudicator revision recording the verdict. Always
    cites ``fresh_contradicting_ids`` so the contradiction is marked adjudicated
    (the producer stops re-firing) — this is what guarantees the loop terminates,
    even when the skeptic is inconclusive."""
    new_confidence = max(0.0, min(1.0, belief.confidence + assessment.suggested_confidence_delta))
    collapses = assessment.verdict == "contradicted" and new_confidence < _refute_floor()
    return [
        ReviseBeliefEffect(
            belief_id=belief.id,
            new_statement=belief.statement,  # adjudication adjusts standing, not wording
            new_confidence=new_confidence,
            revised_by_agent=_ADJUDICATOR_AGENT,
            rationale=f"adjudication ({assessment.verdict}): {assessment.rationale}",
            trigger_claim_ids=fresh_contradicting_ids or list(belief.contradicting_claim_ids),
            set_not_held=collapses,
            recompute_confidence=False,  # the verdict sets confidence, not evidence signals
        )
    ]


@register_skill
class AdjudicateContradictionSkill:
    """Handle ``contradicted_belief`` tensions: gather corroboration, then weigh
    both sides and emit one adjudicator revision (deep, across rounds)."""

    skill_id = "adjudicate-contradiction"
    handles = (TensionKind.contradicted_belief,)

    def __init__(self, llm: Any | None = None) -> None:
        # Injectable for tests; built lazily in run() in production.
        self._llm = llm

    async def run(self, conn: Any, tension: Tension, *, budget_usd: float) -> list[Effect]:
        import asyncio

        from mesh_db.beliefs import get_belief_by_id

        from mesh_agents.skills.investigate_gap import _allowed_source_types

        belief_id = tension.target_ref.get("belief_id")
        if not belief_id:
            return []
        belief = get_belief_by_id(conn, belief_id)
        if belief is None:
            return []

        investigations = _adjudication_investigations(conn, tension.field_id, belief_id)

        # Defensive: a gather sub-step is still in flight (the producer normally
        # suppresses the tension here, but never act mid-gather).
        if any(inv.status not in _TERMINAL for inv in investigations):
            return []

        # ── plan/gather step: no investigation yet → open one ──
        if not investigations:
            allowed = await asyncio.to_thread(_allowed_source_types, conn, tension.field_id)
            inv = _open_gather(belief, tension.field_id, allowed)
            return [OpenInvestigationEffect(field_id=tension.field_id, investigation=inv)]

        # ── reason/decide step: gather terminated → weigh both sides ──
        fresh = [str(c) for c in tension.signals.get("contradicting_claim_ids", [])]
        gathered_ids = sorted(
            {cid for inv in investigations for cid in inv.collected_claim_ids}
        )
        # Belief's own support + the gathered corroboration weighed against the
        # fresh contradictions.
        supporting = _hydrate_claims(
            conn, sorted(set(belief.supporting_claim_ids) | set(gathered_ids))
        )
        contradicting = _hydrate_claims(conn, list(belief.contradicting_claim_ids))
        skeptic_input = SkepticInput(
            belief=BeliefSummary(
                belief_id=belief.id,
                topic=belief.topic,
                statement=belief.statement,
                confidence=belief.confidence,
            ),
            supporting_claims=supporting,
            contradicting_claims=contradicting,
            in_scope_entities=_collect_in_scope_entities(conn, supporting, contradicting),
        )
        llm = self._llm or make_routed_llm_client(agent_name="skeptic")
        assessment, _usage, _model = await asyncio.to_thread(
            challenge_belief_with_memory,
            llm,
            skeptic_input,
            _ADJUDICATOR_AGENT,
            tension.field_id,
            conn,
        )
        return _decide_effects(belief, assessment, fresh, datetime.now(UTC))
