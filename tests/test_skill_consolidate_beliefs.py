"""Skill ``consolidate-beliefs``: redundant-belief tension → MergeBeliefsEffect.

Mirrors ``test_skill_merge_candidate``. Seeds two held, same-family beliefs whose
statements embed identically (cosine 1.0 → the high band auto-merges without an
LLM), runs the skill, and applies the emitted effect through the gateway —
asserting the strictly append-only outcome (the duplicate is absorbed and marked
not-held, no row is deleted).
"""
from __future__ import annotations

import asyncio

from mesh_agents.skills.consolidate_beliefs import ConsolidateBeliefsSkill
from mesh_db.beliefs import create_belief, get_belief_by_id, set_belief_embedding
from mesh_db.connection import MeshConnection
from mesh_db.effects import apply_effects
from mesh_models.belief import Belief
from mesh_models.effect import MergeBeliefsEffect
from mesh_models.field import DEFAULT_FIELD_ID
from mesh_models.tension import Tension, TensionKind


def _unit_vec() -> list[float]:
    return [1.0] + [0.0] * 383


def _held_belief(conn: MeshConnection, topic: str, statement: str) -> Belief:
    belief = Belief(
        topic=topic, statement=statement, confidence=0.5, is_currently_held=True
    )
    create_belief(conn, belief, field_id=DEFAULT_FIELD_ID)
    set_belief_embedding(conn, belief.id, _unit_vec())
    return belief


def _tension(belief_id: str, candidate_id: str, similarity: float) -> Tension:
    kind = TensionKind.redundant_beliefs
    return Tension(
        id=f"{kind.value}:{belief_id}:{candidate_id}",
        field_id=DEFAULT_FIELD_ID,
        kind=kind,
        subject="a ≈ b",
        rationale="redundant",
        value=similarity,
        est_cost_usd=0.02,
        handler_skill="consolidate-beliefs",
        target_ref={"belief_id": belief_id, "candidate_id": candidate_id},
        signals={"candidate_id": candidate_id, "similarity": similarity},
    )


def test_high_band_emits_merge_effect_no_llm(tmp_db: MeshConnection) -> None:
    a = _held_belief(tmp_db, "capability:flownet", "FlowNet is accurate.")
    b = _held_belief(tmp_db, "capability:flownet", "FlowNet is accurate.")
    skill = ConsolidateBeliefsSkill()

    effects = asyncio.run(
        skill.run(tmp_db, _tension(a.id, b.id, 0.99), budget_usd=0.02)
    )
    assert len(effects) == 1
    assert isinstance(effects[0], MergeBeliefsEffect)


def test_low_band_declines(tmp_db: MeshConnection) -> None:
    a = _held_belief(tmp_db, "capability:x", "X works.")
    b = _held_belief(tmp_db, "capability:y", "Y works.")
    skill = ConsolidateBeliefsSkill()
    # Below the reject floor → no effect (and no LLM).
    effects = asyncio.run(
        skill.run(tmp_db, _tension(a.id, b.id, 0.50), budget_usd=0.02)
    )
    assert effects == []


def test_gateway_merge_is_append_only(tmp_db: MeshConnection) -> None:
    a = _held_belief(tmp_db, "capability:flownet", "FlowNet is accurate.")
    b = _held_belief(tmp_db, "capability:flownet", "FlowNet is accurate.")
    skill = ConsolidateBeliefsSkill()

    effects = asyncio.run(
        skill.run(tmp_db, _tension(a.id, b.id, 0.99), budget_usd=0.02)
    )
    report = apply_effects(tmp_db, effects)
    assert report.beliefs_merged == 1

    # Append-only: BOTH rows still exist; exactly one is now not-held (absorbed).
    held = [
        get_belief_by_id(tmp_db, bid).is_currently_held  # type: ignore[union-attr]
        for bid in (a.id, b.id)
    ]
    assert sorted(held) == [False, True]
