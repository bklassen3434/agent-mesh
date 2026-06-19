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

import os
from typing import Any

from mesh_db.beliefs import get_belief_signals, list_beliefs
from mesh_db.claims import unsynthesized_claim_counts_by_entity
from mesh_db.connectors import list_field_connectors
from mesh_db.entities import find_duplicate_candidate_pairs, get_entities_by_ids
from mesh_db.investigations import list_investigations
from mesh_db.sources import get_source_payload, unextracted_sources
from mesh_models.field import DEFAULT_FIELD_ID, DEFAULT_FIELD_SLUG
from mesh_models.investigation import InvestigationStatus
from mesh_models.source import Source
from mesh_models.tension import Agenda, Tension, TensionKind

from mesh_agents.connector import investigate_source_name
from mesh_agents.connector_dispatch import has_connector, has_investigate
from mesh_agents.discovery import GapKind, GapSignal, analyze_field

# Rough per-kind LLM spend (USD) to resolve one tension, and the skill that would
# claim it. Order-of-magnitude estimates — extraction is one cheap call; an
# investigation is search + extract + synthesize. These are the market's cost
# side; calibrate later from the real ``llm_usage`` ledger.
_KIND_COST_USD: dict[TensionKind, float] = {
    TensionKind.unscouted_connector: 0.001,
    TensionKind.unextracted_source: 0.008,
    TensionKind.under_evidenced_entity: 0.05,
    TensionKind.thin_belief: 0.05,
    TensionKind.stale_belief: 0.04,
    TensionKind.rising_topic: 0.05,
    TensionKind.missing_reciprocal_edge: 0.03,
    TensionKind.merge_candidate: 0.02,
    TensionKind.contested_claim: 0.04,
    TensionKind.unsynthesized_claims: 0.05,
    TensionKind.open_investigation: 0.05,
}

_KIND_SKILL: dict[TensionKind, str] = {
    TensionKind.unscouted_connector: "scout-source",
    TensionKind.unextracted_source: "extract-source",
    TensionKind.under_evidenced_entity: "investigate-gap",
    TensionKind.thin_belief: "investigate-gap",
    TensionKind.stale_belief: "challenge-belief",
    TensionKind.rising_topic: "investigate-gap",
    TensionKind.missing_reciprocal_edge: "investigate-gap",
    TensionKind.merge_candidate: "merge-candidate",
    TensionKind.contested_claim: "challenge-belief",
    TensionKind.unsynthesized_claims: "synthesize-belief",
    TensionKind.open_investigation: "dispatch-investigation",
}

# GapKind → TensionKind (the lift-in is 1:1; names already match).
_GAP_TO_TENSION: dict[GapKind, TensionKind] = {
    GapKind.under_evidenced_entity: TensionKind.under_evidenced_entity,
    GapKind.thin_belief: TensionKind.thin_belief,
    GapKind.stale_belief: TensionKind.stale_belief,
    GapKind.rising_topic: TensionKind.rising_topic,
    GapKind.missing_reciprocal_edge: TensionKind.missing_reciprocal_edge,
}


def scout_tensions(conn: Any, field_id: str) -> list[Tension]:
    """One tension per enabled connector that has an in-process scout handler —
    the market's source-acquisition work (→ the scout-source skill). The connector
    config rides in ``signals`` so the skill needs no second read.

    Source acquisition is an *operational*, connector-config-driven concern, not a
    knowledge gap derived from the board, so it lives here (called by the market
    loop) rather than inside ``compute_agenda`` (the board's knowledge-work view,
    also rendered read-only by ``mesh.cli agenda``). The market loop's
    once-per-run dispatch guard keeps it from re-scouting so the field can still
    reach quiescence."""
    kind = TensionKind.unscouted_connector
    out: list[Tension] = []
    for fc in list_field_connectors(conn, field_id, enabled_only=True):
        if not has_connector(fc.connector_id):
            continue
        out.append(
            Tension(
                id=f"{kind.value}:{fc.connector_id}",
                field_id=field_id,
                kind=kind,
                subject=fc.connector_id,
                rationale=f"Poll the enabled '{fc.connector_id}' connector for new sources.",
                # High: fresh material feeds every downstream tension, and it's cheap.
                value=0.6,
                est_cost_usd=_KIND_COST_USD[kind],
                handler_skill=_KIND_SKILL[kind],
                target_ref={"connector_id": fc.connector_id},
                signals={"config": fc.config},
            )
        )
    return out


def investigation_tensions(conn: Any, field_id: str, *, limit: int = 50) -> list[Tension]:
    """One tension per open/in-progress investigation that can still be worked —
    i.e. one whose suggested source types include a connector that is enabled for
    the field AND has an in-process investigate handler (→ the dispatch-investigation
    skill). Operational, like ``scout_tensions``; the market loop calls it.

    Investigations whose sources aren't reachable are skipped (not a tension), so
    the agenda never surfaces work no skill can do."""
    kind = TensionKind.open_investigation
    investigable = {
        investigate_source_name(fc.connector_id)
        for fc in list_field_connectors(conn, field_id, enabled_only=True)
        if has_investigate(investigate_source_name(fc.connector_id))
    }
    if not investigable:
        return []
    out: list[Tension] = []
    for status in (InvestigationStatus.open, InvestigationStatus.in_progress):
        for inv in list_investigations(conn, status=status, limit=limit, field_id=field_id):
            if not (set(inv.suggested_source_types) & investigable):
                continue
            out.append(
                Tension(
                    id=f"{kind.value}:{inv.id}",
                    field_id=field_id,
                    kind=kind,
                    subject=inv.hypothesis or inv.question,
                    rationale=f"Gather evidence for open investigation: {inv.question}",
                    value=0.4 + 0.4 * inv.priority,
                    est_cost_usd=_KIND_COST_USD[kind],
                    handler_skill=_KIND_SKILL[kind],
                    target_ref={"investigation_id": inv.id},
                    signals={"origin": inv.origin.value, "attempts": inv.pipeline_runs_attempted},
                )
            )
    return out


def _tension_from_source(src: Source, field_id: str, payload: dict[str, Any] | None) -> Tension:
    kind = TensionKind.unextracted_source
    # Foundational + cheap: reading what we already have is the lowest-cost way to
    # add knowledge. Nudge by the source's reliability prior so a trusted unread
    # source ranks a touch higher than a sketchy one.
    value = 0.40 + 0.20 * src.reliability_prior
    signals: dict[str, Any] = {
        "source_type": src.type.value,
        "reliability_prior": src.reliability_prior,
    }
    # Carry the scouted content (title/abstract) so extract-source — which reads
    # the paper text from the tension, not the row — can recover it (Phase market).
    # An investigation lineage (set when the source was gathered for one) rides
    # along so extract-source can attach the resulting claims back.
    if payload:
        signals.update({k: payload[k] for k in ("title", "abstract") if k in payload})
        if payload.get("investigation_id"):
            signals["investigation_id"] = payload["investigation_id"]
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
        signals=signals,
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


def _merge_candidate_tensions(conn: Any, field_id: str, *, limit: int) -> list[Tension]:
    """Entity pairs that look like duplicates (→ the merge-candidate skill)."""
    kind = TensionKind.merge_candidate
    min_sim = float(os.environ.get("MESH_ENTITY_MERGE_LOW", "0.80"))
    out: list[Tension] = []
    for id_a, name_a, id_b, name_b, sim in find_duplicate_candidate_pairs(
        conn, field_id=field_id, min_similarity=min_sim, limit=limit
    ):
        out.append(
            Tension(
                id=f"{kind.value}:{id_a}:{id_b}",
                field_id=field_id,
                kind=kind,
                subject=f"{name_a} ≈ {name_b}",
                rationale=(
                    f"'{name_a}' and '{name_b}' look like the same entity "
                    f"(similarity {sim:.2f})."
                ),
                value=sim,  # more confident match → more valuable to resolve
                est_cost_usd=_KIND_COST_USD[kind],
                handler_skill=_KIND_SKILL[kind],
                target_ref={"entity_id": id_a, "candidate_id": id_b},
                signals={"candidate_id": id_b, "similarity": sim},
            )
        )
    return out


def _contested_claim_tensions(conn: Any, field_id: str, *, limit: int) -> list[Tension]:
    """Held beliefs under unresolved challenge (→ the challenge-belief skill)."""
    kind = TensionKind.contested_claim
    out: list[Tension] = []
    for belief in list_beliefs(conn, currently_held=True, limit=limit, field_id=field_id):
        sig = get_belief_signals(conn, belief.id)
        skeptic = int(sig.get("skeptic_counter_claim_count", 0))
        contradictions = len(belief.contradicting_claim_ids)
        if skeptic == 0 and contradictions == 0:
            continue
        out.append(
            Tension(
                id=f"{kind.value}:{belief.id}",
                field_id=field_id,
                kind=kind,
                subject=belief.topic,
                rationale=(
                    f"Belief '{belief.statement}' has {skeptic} skeptic counter-claim(s) "
                    f"and {contradictions} contradiction(s) — re-examine it."
                ),
                value=0.75,
                est_cost_usd=_KIND_COST_USD[kind],
                handler_skill=_KIND_SKILL[kind],
                target_ref={"belief_id": belief.id},
                signals={"skeptic_counter_claims": skeptic, "contradictions": contradictions},
            )
        )
    return out


def _unsynthesized_tensions(conn: Any, field_id: str, *, limit: int) -> list[Tension]:
    """Entities with claims no belief reflects yet (→ the synthesize-belief skill)."""
    kind = TensionKind.unsynthesized_claims
    counts = unsynthesized_claim_counts_by_entity(conn, field_id=field_id, limit=limit)
    names = {e.id: e.canonical_name for e in get_entities_by_ids(conn, [eid for eid, _ in counts])}
    out: list[Tension] = []
    for entity_id, count in counts:
        name = names.get(entity_id, entity_id)
        out.append(
            Tension(
                id=f"{kind.value}:{entity_id}",
                field_id=field_id,
                kind=kind,
                subject=name,
                rationale=f"'{name}' has {count} claim(s) not yet reflected in any belief.",
                value=0.65,
                est_cost_usd=_KIND_COST_USD[kind],
                handler_skill=_KIND_SKILL[kind],
                target_ref={"entity_id": entity_id},
                signals={"unsynthesized_claims": count},
            )
        )
    return out


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
        tensions.append(
            _tension_from_source(src, field_id, get_source_payload(conn, src.id))
        )

    # Knowledge gaps: reuse the existing rule-based analyzer verbatim.
    for gap in analyze_field(conn, field_id, limit=gap_limit):
        if gap.kind not in _GAP_TO_TENSION:
            continue
        tensions.append(_tension_from_gap(gap, field_id))

    # Phase 2a tensions the skill fan-out resolves.
    tensions.extend(_merge_candidate_tensions(conn, field_id, limit=gap_limit))
    tensions.extend(_contested_claim_tensions(conn, field_id, limit=gap_limit))
    tensions.extend(_unsynthesized_tensions(conn, field_id, limit=gap_limit))

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
