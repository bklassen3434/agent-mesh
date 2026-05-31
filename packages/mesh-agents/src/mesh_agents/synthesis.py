"""Type-routed belief synthesis (Phase 14b).

Phase 13 and earlier formed beliefs only from leaderboard ``score`` claims
(see ``sota_tracker``). This module adds the first *entity-anchored* synthesis:
``capability`` claims about a resolved entity converge onto a single belief
keyed ``capability:<entity_id>``, carrying every supporting capability claim as
provenance.

The functions here are pure — the coordinator gathers the full active capability
claim set per touched entity (this run + historical) and the existing belief,
calls in, and owns the write. Rebuilding the statement + provenance from the
*whole* claim set each time makes synthesis converge and stay idempotent:
re-running with no new capability claims yields no update.

``score`` claims keep flowing through ``sota_tracker.update_sota_pure`` unchanged
— this module deliberately does not touch the leaderboard path.
"""
from __future__ import annotations

from pydantic import BaseModel

from mesh_agents.sota_tracker import BeliefUpdate, ResolvedClaim

CAPABILITY_TOPIC_PREFIX = "capability:"

# Cap how many capability phrases render into the belief statement (provenance
# still links every claim). Keeps the statement readable as evidence grows.
_MAX_CAPABILITIES_IN_STATEMENT = 8


def capability_topic(entity_id: str) -> str:
    """Entity-anchored topic for an entity's capability belief."""
    return f"{CAPABILITY_TOPIC_PREFIX}{entity_id}"


class ExistingCapabilityBelief(BaseModel):
    """The current capability belief for an entity, if one exists."""

    belief_id: str
    statement: str
    confidence: float
    supporting_claim_ids: list[str] = []


class CapabilityBeliefInput(BaseModel):
    """One entity's full active capability evidence for synthesis."""

    entity_id: str
    entity_name: str
    claims: list[ResolvedClaim]
    existing_belief: ExistingCapabilityBelief | None = None


def _ordered_unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _format_statement(name: str, capabilities: list[str]) -> str:
    shown = capabilities[:_MAX_CAPABILITIES_IN_STATEMENT]
    more = len(capabilities) - len(shown)
    body = "; ".join(shown)
    if more > 0:
        body += f"; (+{more} more)"
    return f"{name}: {body}"


def synthesize_capability_belief(inp: CapabilityBeliefInput) -> BeliefUpdate | None:
    """Build the belief update for one entity's capability claims.

    Returns ``None`` when there is nothing to assert, or when an existing belief
    is already in sync with the evidence (so re-runs don't churn revisions).
    """
    capability_claims = [c for c in inp.claims if c.claim_type.value == "capability"]
    capabilities = _ordered_unique(
        [str(c.object.get("capability", "")).strip() for c in capability_claims]
    )
    if not capabilities:
        return None

    supporting = _ordered_unique([c.claim_id for c in capability_claims])
    statement = _format_statement(inp.entity_name, capabilities)
    topic = capability_topic(inp.entity_id)

    existing = inp.existing_belief
    if existing is None:
        return BeliefUpdate(
            topic=topic,
            new_statement=statement,
            new_confidence=0.5,  # 14d replaces this with an evidence-derived score
            supporting_claim_ids=supporting,
            rationale=(
                f"Capabilities of {inp.entity_name} synthesized from "
                f"{len(supporting)} claim(s)."
            ),
            is_new_belief=True,
            existing_belief_id=None,
        )

    # Idempotency guard: nothing changed → no revision.
    if statement == existing.statement and set(supporting) == set(
        existing.supporting_claim_ids
    ):
        return None

    return BeliefUpdate(
        topic=topic,
        new_statement=statement,
        new_confidence=existing.confidence,  # confidence recomputed in 14d
        supporting_claim_ids=supporting,
        rationale=(
            f"Updated capabilities of {inp.entity_name} "
            f"({len(supporting)} supporting claim(s))."
        ),
        is_new_belief=False,
        existing_belief_id=existing.belief_id,
    )
