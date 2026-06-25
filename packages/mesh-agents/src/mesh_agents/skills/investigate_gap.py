"""Phase 2 fan-out skill: ``investigate-gap`` — open an investigation for a gap.

Wraps the Discovery analyzer/hypothesis-drafter (``mesh_agents.discovery``) as a
controller skill. When the board surfaces a knowledge-gap tension (an under-evidenced
entity, a thin belief, a rising topic, or a comparison edge with no reciprocal),
this skill turns that one tension into a single, testable ``Investigation``
(``origin='discovery'``) and returns it as **one** ``OpenInvestigationEffect``.

It is a *planner*, not a scout: it never runs a search and — like every skill —
never writes. The investigation it opens is the unit of evidence-gathering the
normal extract → resolve → synthesize path will later fulfil. New knowledge still
flows only through that path; discovery only proposes *what to go find out*.

Construction reuses Discovery verbatim: the gap tension is lifted back into a
``GapSignal``, ``draft_hypotheses`` frames a field-scoped hypothesis (best-effort
LLM; degrades cleanly), and ``build_discovery_investigations`` builds the
``Investigation``. When no LLM/connector is available the skill still emits one
investigation from a deterministic, gap-derived proposal — so a funded tension
always yields exactly one effect. ``field_id`` is threaded everywhere; discovery
never crosses fields.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from mesh_db.connectors import list_field_connectors
from mesh_llm import LLMClient, LLMProviderNotReadyError, make_routed_llm_client
from mesh_models.effect import OpenInvestigationEffect
from mesh_models.investigation import Investigation
from mesh_models.tension import Tension, TensionKind

from mesh_agents.connector import investigate_source_name
from mesh_agents.discovery import (
    DiscoveryProposal,
    GapKind,
    GapSignal,
    build_discovery_investigations,
    discover_max_open,
    draft_hypotheses,
    open_investigations,
)
from mesh_agents.profiles import load_profile
from mesh_agents.skill import register_skill

logger = logging.getLogger(__name__)

# Order-of-magnitude LLM spend to draft + open one investigation (one cheap
# routed call). Matches the agenda's per-kind cost for the gap family.
_EST_COST_USD = 0.05


def _gap_from_tension(tension: Tension) -> GapSignal | None:
    """Lift a gap-family ``Tension`` back into the ``GapSignal`` Discovery emits.

    The tension was originally derived 1:1 from a ``GapSignal`` (names match for
    every handled kind), so the round-trip is lossless for what construction
    needs. Returns ``None`` for a kind this skill doesn't handle."""
    try:
        kind = GapKind(tension.kind.value)
    except ValueError:
        return None
    return GapSignal(
        gap_id=tension.id,  # already "<kind>:<target>" — a stable identity
        kind=kind,
        subject=tension.subject,
        rationale=tension.rationale,
        priority=min(1.0, max(0.0, tension.value)),
        entity_id=tension.target_ref.get("entity_id"),
        belief_id=tension.target_ref.get("belief_id"),
        signals=tension.signals,
    )


def _allowed_source_types(conn: Any, field_id: str) -> list[str]:
    """The investigate sources a field's enabled connectors back — the set
    discovery may suggest. Empty (or unreadable) → drafting is skipped."""
    try:
        return sorted(
            {
                investigate_source_name(fc.connector_id)
                for fc in list_field_connectors(conn, field_id, enabled_only=True)
            }
        )
    except Exception as exc:  # DB-less / unreachable — degrade to the fallback
        logger.debug("allowed_sources_failed", extra={"field_id": field_id, "error": str(exc)})
        return []


def _fallback_proposal(gap: GapSignal) -> DiscoveryProposal:
    """A deterministic, LLM-free proposal so a funded gap always opens exactly one
    investigation, even with no LLM or no enabled connector to suggest."""
    return DiscoveryProposal(
        gap_id=gap.gap_id,
        hypothesis=gap.rationale,
        suggested_source_types=[],
        rationale="opened without an LLM hypothesis (no model/connector available)",
    )


def _plan_investigation(
    conn: Any, llm: LLMClient | None, field_id: str, gap: GapSignal
) -> Investigation:
    """Draft a hypothesis (best-effort LLM) and build one ``Investigation`` for
    ``gap``, reusing Discovery's construction. Synchronous (the LLM call blocks);
    callers run it off the event loop. Never writes; never raises on LLM failure."""
    profile = load_profile(field_id)
    allowed = _allowed_source_types(conn, field_id)
    proposals: list[DiscoveryProposal] = []
    if llm is not None and allowed:
        try:
            proposals, _usage, _model = draft_hypotheses(
                profile, [gap], llm=llm, allowed_source_types=allowed
            )
        except LLMProviderNotReadyError as exc:
            logger.warning("investigate_gap_llm_unavailable", extra={"error": str(exc)})
    if not proposals:
        proposals = [_fallback_proposal(gap)]
    # existing=[] so a dispatched tension always yields one investigation; the controller
    # / write gateway own idempotency (Phase 3), not this skill.
    built = build_discovery_investigations([gap], proposals, existing=[])
    return built[0]


@register_skill
class InvestigateGapSkill:
    """Handle a knowledge-gap tension and open one investigation for it."""

    skill_id = "investigate-gap"
    handles = (
        TensionKind.under_evidenced_entity,
        TensionKind.thin_belief,
        TensionKind.rising_topic,
        TensionKind.missing_reciprocal_edge,
    )

    def __init__(self, llm_factory: Callable[[], LLMClient | None] | None = None) -> None:
        # Injectable for tests; production builds the routed discovery client.
        self._llm_factory = llm_factory

    def _make_llm(self) -> LLMClient | None:
        if self._llm_factory is not None:
            return self._llm_factory()
        try:
            return make_routed_llm_client(agent_name="discovery")
        except Exception as exc:  # construction issues degrade to the fallback path
            logger.debug("investigate_gap_llm_build_failed", extra={"error": str(exc)})
            return None

    async def run(
        self, conn: Any, tension: Tension, *, budget_usd: float
    ) -> list[OpenInvestigationEffect]:
        gap = _gap_from_tension(tension)
        if gap is None:
            return []
        # Throttle: don't keep opening investigations once the open backlog is
        # full — each one drives rate-limited arxiv fetches downstream. Pauses
        # discovery until existing investigations resolve, then resumes.
        if len(open_investigations(conn, field_id=tension.field_id)) >= discover_max_open():
            return []
        llm = self._make_llm()
        investigation = await asyncio.to_thread(
            _plan_investigation, conn, llm, tension.field_id, gap
        )
        return [
            OpenInvestigationEffect(field_id=tension.field_id, investigation=investigation)
        ]
