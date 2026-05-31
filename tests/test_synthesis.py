"""Pure unit tests for type-routed capability synthesis (Phase 14b)."""
from __future__ import annotations

from typing import Any

import pytest
from mesh_agents.sota_tracker import ResolvedClaim
from mesh_agents.synthesis import (
    CapabilityBeliefInput,
    ExistingCapabilityBelief,
    capability_topic,
    edge_for_claim,
    synthesize_capability_belief,
)
from mesh_models.claim import ClaimType


def _cap_claim(claim_id: str, entity_id: str, capability: str) -> ResolvedClaim:
    return ResolvedClaim(
        claim_id=claim_id,
        subject_entity_id=entity_id,
        predicate="has_capability",
        object={"capability": capability},
        source_id="src",
        raw_excerpt=capability,
        confidence=0.9,
    )


def test_claim_type_derived_on_resolved_claim() -> None:
    c = _cap_claim("c1", "e1", "long context")
    assert c.claim_type.value == "capability"


def test_new_capability_belief_links_all_supporting_claims() -> None:
    claims = [
        _cap_claim("c1", "e1", "handles 1M-token context"),
        _cap_claim("c2", "e1", "linear-time inference"),
    ]
    update = synthesize_capability_belief(
        CapabilityBeliefInput(entity_id="e1", entity_name="Mamba", claims=claims)
    )
    assert update is not None
    assert update.is_new_belief
    assert update.topic == capability_topic("e1")
    assert update.topic == "capability:e1"
    assert set(update.supporting_claim_ids) == {"c1", "c2"}
    # Both capabilities surface in the entity-anchored statement.
    assert update.new_statement.startswith("Mamba:")
    assert "1M-token context" in update.new_statement
    assert "linear-time inference" in update.new_statement


def test_duplicate_capabilities_dedup_in_statement() -> None:
    claims = [
        _cap_claim("c1", "e1", "long context"),
        _cap_claim("c2", "e1", "Long Context"),  # case-variant dup
    ]
    update = synthesize_capability_belief(
        CapabilityBeliefInput(entity_id="e1", entity_name="X", claims=claims)
    )
    assert update is not None
    # Only one capability phrase rendered, but both claims kept as provenance.
    assert update.new_statement.count("ong") == 1
    assert set(update.supporting_claim_ids) == {"c1", "c2"}


def test_no_capability_claims_returns_none() -> None:
    score = ResolvedClaim(
        claim_id="s1", subject_entity_id="e1", predicate="achieves_score",
        object={"score": 90, "benchmark": "MMLU"}, source_id="s", raw_excerpt="",
        confidence=0.9,
    )
    update = synthesize_capability_belief(
        CapabilityBeliefInput(entity_id="e1", entity_name="X", claims=[score])
    )
    assert update is None


def test_revision_when_new_evidence_arrives() -> None:
    existing = ExistingCapabilityBelief(
        belief_id="b1",
        statement="Mamba: handles 1M-token context",
        confidence=0.5,
        supporting_claim_ids=["c1"],
    )
    claims = [
        _cap_claim("c1", "e1", "handles 1M-token context"),
        _cap_claim("c2", "e1", "linear-time inference"),
    ]
    update = synthesize_capability_belief(
        CapabilityBeliefInput(
            entity_id="e1", entity_name="Mamba", claims=claims, existing_belief=existing
        )
    )
    assert update is not None
    assert not update.is_new_belief
    assert update.existing_belief_id == "b1"
    assert set(update.supporting_claim_ids) == {"c1", "c2"}


def test_idempotent_no_change_returns_none() -> None:
    claims = [_cap_claim("c1", "e1", "handles 1M-token context")]
    statement = "Mamba: handles 1M-token context"
    existing = ExistingCapabilityBelief(
        belief_id="b1", statement=statement, confidence=0.5,
        supporting_claim_ids=["c1"],
    )
    update = synthesize_capability_belief(
        CapabilityBeliefInput(
            entity_id="e1", entity_name="Mamba", claims=claims, existing_belief=existing
        )
    )
    assert update is None  # nothing changed → no revision churn


# --- relational edge mapping (14c) ----------------------------------------


@pytest.mark.parametrize(
    ("claim_type", "object", "expected"),
    [
        (ClaimType.comparison, {"compared_to": "GPT-3", "on": "MMLU"},
         ("outperforms", "GPT-3")),
        (ClaimType.attribution, {"lab": "Meta AI"}, ("developed_by", "Meta AI")),
        (ClaimType.lineage, {"parent": "Transformer"}, ("based_on", "Transformer")),
        (ClaimType.evaluation, {"benchmark": "MMLU"}, ("evaluated_on", "MMLU")),
    ],
)
def test_edge_for_claim_maps_relational_types(
    claim_type: ClaimType, object: dict[str, Any], expected: tuple[str, str]
) -> None:
    assert edge_for_claim(claim_type, object) == expected


def test_edge_for_claim_non_relational_is_none() -> None:
    assert edge_for_claim(ClaimType.capability, {"capability": "x"}) is None
    assert edge_for_claim(ClaimType.score, {"score": 90, "benchmark": "MMLU"}) is None
    assert edge_for_claim(ClaimType.reproduction, {"target": "x"}) is None


def test_edge_for_claim_missing_target_is_none() -> None:
    assert edge_for_claim(ClaimType.comparison, {"on": "MMLU"}) is None
    assert edge_for_claim(ClaimType.attribution, {"lab": "   "}) is None
