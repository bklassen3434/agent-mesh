"""Phase 0 of the agentic migration: compute the self-writing to-do list.

``compute_agenda`` reads the current knowledge board and returns a ranked
``Agenda`` of ``Tension``s — what an agentic mesh *would* choose to work on right
now. It writes nothing and calls no LLM: operational tensions come from a single
anti-join (unread sources), knowledge-gap tensions are lifted from the existing
rule-based ``analyze_field`` (the Discovery analyzer), and the "market" is a
greedy knapsack that funds the highest value-per-dollar tensions under a budget.

This is the de-risking step: if the ranking looks sensible against real data, the
value function — the heart of the future market — is sound, and the rest of the
architecture (skills, market loop, write gateway) can be built with confidence.

Mapping from the existing ``GapKind`` to the unified ``TensionKind`` is 1:1; the
only genuinely new kind is ``unextracted_source``. Each kind carries a rough
per-kind cost and the name of the skill that would eventually claim it, so the
agenda doubles as a board→skill map.
"""
from __future__ import annotations

from typing import Any

from mesh_db.sources import unextracted_sources
from mesh_models.field import DEFAULT_FIELD_ID, DEFAULT_FIELD_SLUG
from mesh_models.source import Source
from mesh_models.tension import Agenda, Tension, TensionKind

from mesh_agents.discovery import GapKind, GapSignal, analyze_field

# Rough per-kind LLM spend (USD) to resolve one tension, and the skill that would
# claim it. Order-of-magnitude estimates — extraction is one cheap call; an
# investigation is search + extract + synthesize. These are the market's cost
# side; calibrate later from the real ``llm_usage`` ledger.
_KIND_COST_USD: dict[TensionKind, float] = {
    TensionKind.unextracted_source: 0.008,
    TensionKind.under_evidenced_entity: 0.05,
    TensionKind.thin_belief: 0.05,
    TensionKind.stale_belief: 0.04,
    TensionKind.rising_topic: 0.05,
    TensionKind.missing_reciprocal_edge: 0.03,
}

_KIND_SKILL: dict[TensionKind, str] = {
    TensionKind.unextracted_source: "extract-source",
    TensionKind.under_evidenced_entity: "investigate-gap",
    TensionKind.thin_belief: "investigate-gap",
    TensionKind.stale_belief: "challenge-belief",
    TensionKind.rising_topic: "investigate-gap",
    TensionKind.missing_reciprocal_edge: "investigate-gap",
}

# GapKind → TensionKind (the lift-in is 1:1; names already match).
_GAP_TO_TENSION: dict[GapKind, TensionKind] = {
    GapKind.under_evidenced_entity: TensionKind.under_evidenced_entity,
    GapKind.thin_belief: TensionKind.thin_belief,
    GapKind.stale_belief: TensionKind.stale_belief,
    GapKind.rising_topic: TensionKind.rising_topic,
    GapKind.missing_reciprocal_edge: TensionKind.missing_reciprocal_edge,
}


def _tension_from_source(src: Source, field_id: str) -> Tension:
    kind = TensionKind.unextracted_source
    # Foundational + cheap: reading what we already have is the lowest-cost way to
    # add knowledge. Nudge by the source's reliability prior so a trusted unread
    # source ranks a touch higher than a sketchy one.
    value = 0.40 + 0.20 * src.reliability_prior
    return Tension(
        id=f"{kind.value}:{src.id}",
        field_id=field_id,
        kind=kind,
        subject=src.url,
        rationale=f"{src.type.value} source has no extracted claims yet — unread.",
        value=value,
        est_cost_usd=_KIND_COST_USD[kind],
        handler_skill=_KIND_SKILL[kind],
        target_ref={"source_id": src.id},
        signals={"source_type": src.type.value, "reliability_prior": src.reliability_prior},
    )


def _tension_from_gap(gap: GapSignal, field_id: str) -> Tension:
    kind = _GAP_TO_TENSION[gap.kind]
    target_ref: dict[str, str] = {}
    if gap.belief_id:
        target_ref["belief_id"] = gap.belief_id
    if gap.entity_id:
        target_ref["entity_id"] = gap.entity_id
    return Tension(
        id=gap.gap_id,  # already "<kind>:<target>", a stable identity
        field_id=field_id,
        kind=kind,
        subject=gap.subject,
        rationale=gap.rationale,
        value=gap.priority,  # GapSignal's priority IS the value estimate
        est_cost_usd=_KIND_COST_USD[kind],
        handler_skill=_KIND_SKILL[kind],
        target_ref=target_ref,
        signals=gap.signals,
    )


def compute_agenda(
    conn: Any,
    field_id: str = DEFAULT_FIELD_ID,
    *,
    field_slug: str = DEFAULT_FIELD_SLUG,
    budget_usd: float = 0.50,
    value_floor: float = 0.0,
    gap_limit: int = 20,
    source_limit: int = 50,
) -> Agenda:
    """Read the board, score every tension, and clear a budget — all read-only.

    Returns an ``Agenda`` whose ``tensions`` are sorted by value-per-dollar and
    whose ``funded_ids`` are what a greedy market would fund under ``budget_usd``.
    No writes, no LLM, field-scoped (every reader is passed ``field_id``)."""
    tensions: list[Tension] = []

    # Operational: papers/posts we have but haven't read (cheap, high-leverage).
    for src in unextracted_sources(conn, field_id=field_id, limit=source_limit):
        tensions.append(_tension_from_source(src, field_id))

    # Knowledge gaps: reuse the existing rule-based analyzer verbatim.
    for gap in analyze_field(conn, field_id, limit=gap_limit):
        if gap.kind not in _GAP_TO_TENSION:
            continue
        tensions.append(_tension_from_gap(gap, field_id))

    # Rank by value-per-dollar (the market's knapsack density).
    tensions.sort(key=lambda t: t.score, reverse=True)

    # Greedy clearing: fund top-down while the budget lasts. Below the cut line
    # the tension simply waits for the next round.
    funded_ids: list[str] = []
    spent = 0.0
    for t in tensions:
        if t.value < value_floor:
            continue
        if spent + t.est_cost_usd <= budget_usd:
            funded_ids.append(t.id)
            spent += t.est_cost_usd

    return Agenda(
        field_id=field_id,
        field_slug=field_slug,
        budget_usd=budget_usd,
        value_floor=value_floor,
        tensions=tensions,
        funded_ids=funded_ids,
    )
