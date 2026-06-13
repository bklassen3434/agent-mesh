"""Phase 22c: proactive, whole-field gap/trend analyzer + hypothesis drafter.

The Curator is the *reactive* path — it inspects one held belief at a time and,
when it looks thin/stale/contested, suggests an Investigation. Discovery is the
*proactive* path: it steps back and looks at the **whole field** — every entity,
belief, claim-velocity trend, and relationship edge — and asks "given everything
we know and what's trending, what should we go find out next?".

Two stages, mirroring the Curator's rule-based posture then adding one LLM pass:

* ``analyze_field`` — RULE-BASED, no LLM. Mines the field's state with existing
  readers into ``GapSignal``s: under-evidenced entities (zero/one claim),
  thin/stale beliefs (low source diversity, old evidence), rising-activity topics
  (claim velocity), and comparison edges missing their reciprocal.
* ``draft_hypotheses`` — ONE LLM pass. Turns the top gap signals into concrete,
  testable investigation hypotheses framed by the field's ``FieldProfile``. It
  proposes *what to search for*, never asserts answers, and degrades to an empty
  list on any LLM failure (never crashes the sweep).

Discovery proposes evidence-gathering, never facts: the output is
``Investigation`` models (built by ``build_discovery_investigations``, deduped
against existing open ones) that the sweep persists under ``mesh_writer`` — new
knowledge still flows only through the normal extract → resolve → synthesize path.
Everything is field-scoped; a field's discovery never reads or seeds another.
"""
from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from mesh_db.beliefs import find_stale_beliefs, get_belief_signals, list_beliefs
from mesh_db.claims import recent_claim_counts_by_entity
from mesh_db.entities import get_entities_by_ids, under_evidenced_entities
from mesh_db.relationships import list_relationships
from mesh_llm import LLMClient, LLMResponseError, LLMUsage
from mesh_llm.prompts import build_discovery_system
from mesh_models.field import DEFAULT_FIELD_ID, FieldProfile
from mesh_models.investigation import Investigation, InvestigationOrigin, InvestigationStatus
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Comparison relationship types whose one-directional presence (A→B with no
# B→A) flags a head-to-head the mesh hasn't examined from the other side.
_COMPARISON_EDGE_TYPES = {"outperforms"}

# Per-kind base priority (0..1). Stale/rising lead — they decay or move fastest.
_KIND_PRIORITY: dict[str, float] = {
    "stale_belief": 0.70,
    "rising_topic": 0.65,
    "thin_belief": 0.60,
    "under_evidenced_entity": 0.55,
    "missing_reciprocal_edge": 0.45,
}


class GapKind(StrEnum):
    under_evidenced_entity = "under_evidenced_entity"
    thin_belief = "thin_belief"
    stale_belief = "stale_belief"
    rising_topic = "rising_topic"
    missing_reciprocal_edge = "missing_reciprocal_edge"


class GapSignal(BaseModel):
    """A machine-detected coverage gap or emerging trend. Carries the triggering
    signals + a machine rationale so an opened Investigation is explainable."""

    gap_id: str
    kind: GapKind
    subject: str  # human label (entity name, belief topic, "A vs B")
    rationale: str  # machine-readable "why this is a gap"
    priority: float = Field(default=0.5, ge=0.0, le=1.0)
    entity_id: str | None = None
    belief_id: str | None = None
    signals: dict[str, Any] = Field(default_factory=dict)


class DiscoveryProposal(BaseModel):
    """One LLM-drafted, testable investigation hypothesis addressing a gap."""

    gap_id: str
    hypothesis: str
    suggested_source_types: list[str] = Field(default_factory=list)
    rationale: str = ""


class DiscoveryProposals(BaseModel):
    """Structured-output wrapper for the hypothesis-drafting LLM call."""

    proposals: list[DiscoveryProposal] = Field(default_factory=list)


# ── Stage 1: rule-based gap/trend analysis ───────────────────────────────────


def analyze_field(
    conn: Any,
    field_id: str = DEFAULT_FIELD_ID,
    *,
    limit: int = 20,
    stale_threshold_days: int = 45,
    rising_since_days: int = 14,
    rising_min_claims: int = 3,
) -> list[GapSignal]:
    """Mine the field's current state into ranked ``GapSignal``s (no LLM).

    Field-scoped end to end: every reader is passed ``field_id`` and discovery
    never crosses fields. Returns at most ``limit`` signals, highest-priority
    first."""
    gaps: list[GapSignal] = []

    # 1. Under-evidenced entities (zero/one claim) — the mesh barely knows them.
    for entity, claim_count in under_evidenced_entities(
        conn, field_id=field_id, max_claims=1, limit=limit
    ):
        gaps.append(
            GapSignal(
                gap_id=f"under_evidenced_entity:{entity.id}",
                kind=GapKind.under_evidenced_entity,
                subject=entity.canonical_name,
                rationale=(
                    f"Entity '{entity.canonical_name}' ({entity.type.value}) has "
                    f"only {claim_count} claim(s) — under-evidenced."
                ),
                priority=_KIND_PRIORITY["under_evidenced_entity"],
                entity_id=entity.id,
                signals={"claim_count": claim_count},
            )
        )

    # 2. Thin beliefs — held but low source diversity / few supporters.
    for belief in list_beliefs(conn, currently_held=True, limit=limit, field_id=field_id):
        sig = get_belief_signals(conn, belief.id)
        diversity = int(sig.get("source_type_diversity", 0))
        supporters = len(belief.supporting_claim_ids)
        if diversity <= 1 and supporters < 2:
            gaps.append(
                GapSignal(
                    gap_id=f"thin_belief:{belief.id}",
                    kind=GapKind.thin_belief,
                    subject=belief.topic,
                    rationale=(
                        f"Belief '{belief.statement}' rests on {supporters} "
                        f"supporter(s) across {diversity} source type(s) — thin."
                    ),
                    priority=_KIND_PRIORITY["thin_belief"],
                    belief_id=belief.id,
                    signals={"source_type_diversity": diversity, "supporters": supporters},
                )
            )

    # 3. Stale beliefs — newest supporting/contradicting claim is old.
    for belief in find_stale_beliefs(
        conn, threshold_days=stale_threshold_days, limit=limit, field_id=field_id
    ):
        gaps.append(
            GapSignal(
                gap_id=f"stale_belief:{belief.id}",
                kind=GapKind.stale_belief,
                subject=belief.topic,
                rationale=(
                    f"Belief '{belief.statement}' has no fresh evidence in "
                    f">{stale_threshold_days}d — re-check it."
                ),
                priority=_KIND_PRIORITY["stale_belief"],
                belief_id=belief.id,
                signals={"stale_threshold_days": stale_threshold_days},
            )
        )

    # 4. Rising topics — high recent claim velocity around an entity.
    velocity = recent_claim_counts_by_entity(
        conn, field_id=field_id, since_days=rising_since_days, limit=limit
    )
    rising_ids = [eid for eid, count in velocity if count >= rising_min_claims]
    rising_names = {
        e.id: e.canonical_name for e in get_entities_by_ids(conn, rising_ids)
    }
    for eid, count in velocity:
        if count < rising_min_claims or eid not in rising_names:
            continue
        name = rising_names[eid]
        gaps.append(
            GapSignal(
                gap_id=f"rising_topic:{eid}",
                kind=GapKind.rising_topic,
                subject=name,
                rationale=(
                    f"'{name}' drew {count} claims in {rising_since_days}d — a "
                    "rising trend worth sampling more deeply."
                ),
                priority=_KIND_PRIORITY["rising_topic"],
                entity_id=eid,
                signals={"claims_recent": count, "window_days": rising_since_days},
            )
        )

    # 5. Comparison edges missing their reciprocal (A→B but no B→A).
    gaps.extend(_missing_reciprocal_edges(conn, field_id, limit=limit))

    gaps.sort(key=lambda g: g.priority, reverse=True)
    return gaps[:limit]


def _missing_reciprocal_edges(
    conn: Any, field_id: str, *, limit: int
) -> list[GapSignal]:
    edges = list_relationships(conn, field_id=field_id, limit=200)
    present = {(e.from_entity_id, e.to_entity_id) for e in edges}
    flagged: list[GapSignal] = []
    name_ids: set[str] = set()
    candidates: list[tuple[str, str]] = []
    for e in edges:
        if e.type not in _COMPARISON_EDGE_TYPES:
            continue
        if (e.to_entity_id, e.from_entity_id) in present:
            continue
        candidates.append((e.from_entity_id, e.to_entity_id))
        name_ids.update((e.from_entity_id, e.to_entity_id))
        if len(candidates) >= limit:
            break
    names = {ent.id: ent.canonical_name for ent in get_entities_by_ids(conn, list(name_ids))}
    for from_id, to_id in candidates:
        a, b = names.get(from_id, from_id), names.get(to_id, to_id)
        flagged.append(
            GapSignal(
                gap_id=f"missing_reciprocal_edge:{to_id}:{from_id}",
                kind=GapKind.missing_reciprocal_edge,
                subject=f"{b} vs {a}",
                rationale=(
                    f"We have '{a} outperforms {b}' but nothing the other way — "
                    f"look for head-to-head evidence from {b}'s side."
                ),
                priority=_KIND_PRIORITY["missing_reciprocal_edge"],
                entity_id=to_id,
                signals={"from_entity_id": from_id, "to_entity_id": to_id},
            )
        )
    return flagged


# ── Stage 2: LLM hypothesis drafting ─────────────────────────────────────────


def _format_gaps_user(gaps: list[GapSignal], allowed_source_types: list[str]) -> str:
    lines = [
        "ALLOWED SOURCES (pick suggested_source_types only from these): "
        + (", ".join(sorted(allowed_source_types)) or "(none)"),
        "",
        "GAPS AND TRENDS:",
    ]
    for g in gaps:
        lines.append(f"- gap_id={g.gap_id} [{g.kind.value}] subject={g.subject}")
        lines.append(f"  why: {g.rationale}")
    lines.append("")
    lines.append(
        "Draft a testable investigation hypothesis for each gap you can address. "
        "Return JSON matching the schema."
    )
    return "\n".join(lines)


def draft_hypotheses(
    profile: FieldProfile,
    gaps: list[GapSignal],
    *,
    llm: LLMClient,
    allowed_source_types: list[str],
    max_hypotheses: int = 10,
) -> tuple[list[DiscoveryProposal], LLMUsage | None, str]:
    """Turn the top gap signals into concrete, testable, field-framed hypotheses.

    Returns ``(proposals, usage, model)``. Conservative: proposals referencing an
    unknown gap are dropped, and ``suggested_source_types`` are intersected with
    ``allowed_source_types`` (a field's enabled connectors) — discovery never
    invents a source. Degrades to ``([], None, "")`` on any LLM parse/format
    failure so one bad pass never crashes the sweep."""
    if not gaps or not allowed_source_types:
        return [], None, ""
    top = gaps[:max_hypotheses]
    valid_ids = {g.gap_id for g in top}
    allowed = set(allowed_source_types)
    system = build_discovery_system(profile)
    user = _format_gaps_user(top, allowed_source_types)
    try:
        result, _latency, usage = llm.complete_with_usage(
            name="draft_hypotheses",
            system=system,
            user=user,
            response_model=DiscoveryProposals,
        )
    except LLMResponseError as exc:
        logger.warning("discovery_draft_parse_failed", extra={"error": str(exc)})
        return [], None, ""
    if not isinstance(result, DiscoveryProposals):
        return [], None, ""

    out: list[DiscoveryProposal] = []
    for p in result.proposals:
        if p.gap_id not in valid_ids or not p.hypothesis.strip():
            continue
        sources = [s for s in p.suggested_source_types if s in allowed]
        if not sources:
            continue
        out.append(
            DiscoveryProposal(
                gap_id=p.gap_id,
                hypothesis=p.hypothesis.strip(),
                suggested_source_types=sources,
                rationale=p.rationale.strip(),
            )
        )
    # usage.model is the realized model (correct under cheap→strong routing
    # escalation); fall back to the client attribute if unset.
    model = usage.model or getattr(llm, "model", "")
    return out, usage, model


# ── Proposal → Investigation (deduped, never written here) ───────────────────


def _gap_dedup_key(gap: GapSignal) -> tuple[str, ...]:
    """A coarse identity for the thing a gap is about, so two passes (or the
    Curator) don't open overlapping investigations."""
    if gap.belief_id:
        return ("belief", gap.belief_id)
    if gap.kind == GapKind.missing_reciprocal_edge:
        return (
            "edge",
            str(gap.signals.get("from_entity_id")),
            str(gap.signals.get("to_entity_id")),
        )
    if gap.entity_id:
        return ("entity", gap.entity_id)
    return ("gap", gap.gap_id)


def _existing_dedup_keys(existing: list[Investigation]) -> set[tuple[str, ...]]:
    keys: set[tuple[str, ...]] = set()
    for inv in existing:
        if inv.status not in (InvestigationStatus.open, InvestigationStatus.in_progress):
            continue
        if inv.opened_by_belief_id:
            keys.add(("belief", inv.opened_by_belief_id))
        if inv.target_entity_id:
            keys.add(("entity", inv.target_entity_id))
    return keys


def build_discovery_investigations(
    gaps: list[GapSignal],
    proposals: list[DiscoveryProposal],
    existing: list[Investigation],
) -> list[Investigation]:
    """Map LLM proposals to ``Investigation`` models (``origin='discovery'``),
    deduped against already-open/recent investigations covering the same gap and
    against each other within the batch. Pure — the sweep persists the result."""
    gaps_by_id = {g.gap_id: g for g in gaps}
    seen = _existing_dedup_keys(existing)
    out: list[Investigation] = []
    for p in proposals:
        gap = gaps_by_id.get(p.gap_id)
        if gap is None:
            continue
        key = _gap_dedup_key(gap)
        # belief/entity keys collapse so an entity-edge gap and an entity gap on
        # the same entity don't both open; check the entity sub-key too.
        entity_key = ("entity", gap.entity_id) if gap.entity_id else None
        if key in seen or (entity_key is not None and entity_key in seen):
            continue
        seen.add(key)
        if entity_key is not None:
            seen.add(entity_key)
        out.append(
            Investigation(
                question=p.hypothesis,
                hypothesis=p.hypothesis,
                target_entity_id=gap.entity_id,
                suggested_source_types=p.suggested_source_types,
                opened_by_belief_id=gap.belief_id,
                related_entity_ids=[gap.entity_id] if gap.entity_id else [],
                origin=InvestigationOrigin.discovery,
                trigger_rationale=f"{gap.rationale} | {p.rationale}".strip(" |"),
                priority=gap.priority,
            )
        )
    return out
