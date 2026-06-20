"""Phase 0 of the agentic migration: the self-writing to-do list.

A ``Tension`` is one unit of "something on the knowledge board needs attention":
a paper that hasn't been read, a belief resting on a single source, an entity the
mesh barely knows, a head-to-head with no return match. Tensions are *derived*
from the store (never stored), exactly like ``GapSignal`` — this module just gives
them a single shape so the controller has one thing to rank and the
skills have one thing to claim.

This is intentionally read-only and additive. Computing an ``Agenda`` writes
nothing and calls no LLM; it only reads what the store already knows and scores
it. If the ranking looks sensible against real data, the rest of the agentic
architecture (skills + controller + write gateway) is sound. If it doesn't, we found
out for the price of a few SELECTs.

Naming note: ``Tension`` is the generalization of ``GapSignal``. Discovery's
``GapSignal`` covers knowledge *gaps* (thin/stale/under-evidenced/trends);
``Tension`` also covers operational work (unread sources) and is the unit the
controller acts on. The two converged once skills landed.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, computed_field


class TensionKind(StrEnum):
    """What kind of attention the board is asking for. Each kind maps 1:1 to the
    skill that would resolve it (``Tension.handler_skill``)."""

    # Operational — cheap, foundational work.
    unscouted_connector = "unscouted_connector"  # an enabled connector to poll
    unextracted_source = "unextracted_source"  # a source we have but haven't read
    # Knowledge gaps — the ``analyze_field`` (GapSignal) family, lifted in.
    under_evidenced_entity = "under_evidenced_entity"
    thin_belief = "thin_belief"
    stale_belief = "stale_belief"
    rising_topic = "rising_topic"
    missing_reciprocal_edge = "missing_reciprocal_edge"
    # Phase 2a — kinds the skill fan-out resolves.
    merge_candidate = "merge_candidate"  # two entities look like duplicates
    redundant_beliefs = "redundant_beliefs"  # two held beliefs say the same thing
    contested_claim = "contested_claim"  # a held belief is under challenge
    unsynthesized_claims = "unsynthesized_claims"  # claims no belief reflects yet
    # An open investigation whose evidence still needs to be gathered.
    open_investigation = "open_investigation"
    # Periodic, LLM-free maintenance — cooldown-gated like scouting (the temporal
    # condition is a stored-timestamp state condition), not board-state driven.
    aging_belief = "aging_belief"  # the held corpus is due for decay + archival
    consolidatable_memory = "consolidatable_memory"  # episodic history due to distil


class Tension(BaseModel):
    """One item on the agenda: a thing worth doing, with its two signal numbers.

    ``value`` is how much resolving this would improve the knowledge base (0..1+,
    higher = more valuable). ``est_cost_usd`` is the rough LLM spend to handle it.
    ``value`` and ``est_cost_usd`` are informational signals the rules may consult
    (and the read-only ``mesh.cli agenda`` view ranks by ``score``); the controller
    itself selects/prioritises via its rule table, not a price. Nothing here is
    written; a Tension is recomputed from board state every round."""

    id: str  # stable identity: "<kind>:<target>" — same board state → same id
    field_id: str
    kind: TensionKind
    subject: str  # human label (entity name, belief topic, "A vs B", source url)
    rationale: str  # machine-readable "why this is on the list"
    value: float = Field(ge=0.0)
    est_cost_usd: float = Field(gt=0.0)
    handler_skill: str  # the skill that would claim this (board → skill mapping)
    target_ref: dict[str, str] = Field(default_factory=dict)  # {entity_id|belief_id|source_id}
    signals: dict[str, Any] = Field(default_factory=dict)  # the triggering measurements

    @computed_field  # type: ignore[prop-decorator]
    @property
    def score(self) -> float:
        """Value per dollar — the read-only agenda view's ranking key."""
        return self.value / self.est_cost_usd


class Agenda(BaseModel):
    """The full ranked to-do list for one field, plus what a budget would fund.

    ``tensions`` are sorted by ``score`` descending. ``funded_ids`` is the greedy
    agenda clearing: walk the list top-down, fund each tension whose cost still
    fits the budget. Everything below the cut line stays for next round — nothing
    is lost, just deferred. ``quiescent`` is the natural stop signal: when no
    tension clears the value floor, the field is settled and the system sleeps."""

    field_id: str
    field_slug: str
    budget_usd: float
    value_floor: float = 0.0
    tensions: list[Tension] = Field(default_factory=list)
    funded_ids: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> int:
        return len(self.tensions)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def funded_count(self) -> int:
        return len(self.funded_ids)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def funded_cost_usd(self) -> float:
        funded = set(self.funded_ids)
        return round(sum(t.est_cost_usd for t in self.tensions if t.id in funded), 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def quiescent(self) -> bool:
        """No tension worth acting on — the field is settled (the stop signal)."""
        return not any(t.value >= self.value_floor for t in self.tensions) or not self.tensions
