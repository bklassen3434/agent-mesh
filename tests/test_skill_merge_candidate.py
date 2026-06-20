"""Tests for the ``merge-candidate`` skill (Phase 2 fan-out).

Exercises the decision bands (auto-merge / auto-reject / LLM adjudicate) with a
mocked LLM, asserts the skill only ever *returns* a ``MergeEntitiesEffect`` (never
merges directly), and runs the full vertical slice through the write gateway.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from mesh_agents.entity_resolution import EntityMatchDecision
from mesh_agents.skill import clear_registry, get_skill, load_builtin_skills
from mesh_agents.skills.merge_candidate import MergeCandidateSkill
from mesh_db.claims import create_claim
from mesh_db.connection import MeshConnection
from mesh_db.effects import apply_effects
from mesh_db.entities import count_entities, create_entity, get_entity_by_id
from mesh_db.sources import create_source
from mesh_models.claim import Claim
from mesh_models.effect import MergeEntitiesEffect
from mesh_models.entity import Entity, EntityType
from mesh_models.field import DEFAULT_FIELD_ID
from mesh_models.source import Source, SourceType
from mesh_models.tension import Tension, TensionKind


class _FakeLLM:
    """Minimal LLMClient stub: returns a fixed adjudication decision."""

    model = "fake"

    def __init__(self, decision: EntityMatchDecision) -> None:
        self._decision = decision

    def complete_with_latency(self, **kwargs: Any) -> tuple[Any, int]:
        return self._decision, 0


def _source(conn: MeshConnection) -> str:
    src = Source(
        type=SourceType.arxiv,
        url="https://arxiv.org/abs/merge-test",
        published_at=datetime.now(UTC),
        raw_content_hash="hash-merge-test",
    )
    create_source(conn, src)
    return src.id


def _entity(conn: MeshConnection, name: str) -> Entity:
    ent = Entity(canonical_name=name, type=EntityType.model)
    create_entity(conn, ent)
    return ent


def _claim(conn: MeshConnection, entity_id: str, source_id: str, predicate: str) -> None:
    create_claim(
        conn,
        Claim(
            predicate=predicate,
            subject_entity_id=entity_id,
            object={"value": predicate},
            source_id=source_id,
            extracted_by_agent="test",
            raw_excerpt=f"excerpt {predicate}",
            confidence=0.7,
        ),
    )


def _tension(entity_id: str, candidate_id: str, similarity: float) -> Tension:
    return Tension(
        id=f"merge_candidate:{entity_id}:{candidate_id}",
        field_id=DEFAULT_FIELD_ID,
        kind=TensionKind.merge_candidate,
        subject="A ≈ B",
        rationale="look-alikes",
        value=similarity,
        est_cost_usd=0.02,
        handler_skill="merge-candidate",
        target_ref={"entity_id": entity_id, "candidate_id": candidate_id},
        signals={"candidate_id": candidate_id, "similarity": similarity},
    )


def test_registered_via_load_builtin_skills() -> None:
    # The module is imported at file load (decorator already ran), so clear the
    # registry and reload to re-run @register_skill from a clean slate — exactly
    # what load_builtin_skills relies on at startup.
    import importlib

    import mesh_agents.skills.merge_candidate as mod

    clear_registry()
    importlib.reload(mod)
    try:
        load_builtin_skills()
        skill = get_skill("merge-candidate")
        assert skill is not None
        assert TensionKind.merge_candidate in skill.handles
    finally:
        clear_registry()
        importlib.reload(mod)


def test_high_band_auto_merges_without_llm(tmp_db: MeshConnection) -> None:
    src = _source(tmp_db)
    a = _entity(tmp_db, "Mamba")
    b = _entity(tmp_db, "Mamba (SSM)")
    # b is the most-claimed → canonical per choose_canonical.
    _claim(tmp_db, b.id, src, "p1")
    _claim(tmp_db, b.id, src, "p2")

    skill = MergeCandidateSkill()  # no LLM — high band must not need one
    effects = asyncio.run(skill.run(tmp_db, _tension(a.id, b.id, 0.99), budget_usd=0.02))

    assert len(effects) == 1
    eff = effects[0]
    assert isinstance(eff, MergeEntitiesEffect)
    assert eff.canonical_id == b.id
    assert eff.duplicate_id == a.id
    # The skill itself wrote nothing — both entities still exist.
    assert get_entity_by_id(tmp_db, a.id) is not None
    assert get_entity_by_id(tmp_db, b.id) is not None


def test_low_band_rejects(tmp_db: MeshConnection) -> None:
    a = _entity(tmp_db, "Mamba")
    b = _entity(tmp_db, "Transformer")
    skill = MergeCandidateSkill(llm=_FakeLLM(EntityMatchDecision(same_entity=True)))  # type: ignore[arg-type]
    effects = asyncio.run(skill.run(tmp_db, _tension(a.id, b.id, 0.5), budget_usd=0.02))
    assert effects == []


def test_middle_band_adjudicates_same_emits_effect(tmp_db: MeshConnection) -> None:
    src = _source(tmp_db)
    a = _entity(tmp_db, "Mamba")
    b = _entity(tmp_db, "Mamba2")
    _claim(tmp_db, a.id, src, "p1")  # a most-claimed → canonical
    skill = MergeCandidateSkill(
        llm=_FakeLLM(EntityMatchDecision(same_entity=True, reason="same SSM"))  # type: ignore[arg-type]
    )
    effects = asyncio.run(skill.run(tmp_db, _tension(a.id, b.id, 0.85), budget_usd=0.02))
    assert len(effects) == 1
    eff = effects[0]
    assert isinstance(eff, MergeEntitiesEffect)
    assert eff.canonical_id == a.id
    assert eff.duplicate_id == b.id


def test_middle_band_adjudicates_not_same_returns_empty(tmp_db: MeshConnection) -> None:
    a = _entity(tmp_db, "Mamba")
    b = _entity(tmp_db, "Mamba2")
    skill = MergeCandidateSkill(llm=_FakeLLM(EntityMatchDecision(same_entity=False)))  # type: ignore[arg-type]
    effects = asyncio.run(skill.run(tmp_db, _tension(a.id, b.id, 0.85), budget_usd=0.02))
    assert effects == []


def test_middle_band_without_llm_is_conservative(tmp_db: MeshConnection) -> None:
    a = _entity(tmp_db, "Mamba")
    b = _entity(tmp_db, "Mamba2")
    skill = MergeCandidateSkill()  # no injected LLM, none available → no merge
    effects = asyncio.run(skill.run(tmp_db, _tension(a.id, b.id, 0.85), budget_usd=0.02))
    assert effects == []


def test_missing_entity_returns_empty(tmp_db: MeshConnection) -> None:
    a = _entity(tmp_db, "Mamba")
    skill = MergeCandidateSkill()
    effects = asyncio.run(
        skill.run(tmp_db, _tension(a.id, "nonexistent-id", 0.99), budget_usd=0.02)
    )
    assert effects == []


def test_full_slice_gateway_applies_merge(tmp_db: MeshConnection) -> None:
    src = _source(tmp_db)
    a = _entity(tmp_db, "Mamba")
    b = _entity(tmp_db, "Mamba (SSM)")
    _claim(tmp_db, b.id, src, "p1")

    before = count_entities(tmp_db, field_id=DEFAULT_FIELD_ID)
    skill = MergeCandidateSkill()
    effects = asyncio.run(skill.run(tmp_db, _tension(a.id, b.id, 0.99), budget_usd=0.02))
    report = apply_effects(tmp_db, effects)

    assert report.entities_merged == 1
    assert count_entities(tmp_db, field_id=DEFAULT_FIELD_ID) == before - 1
    # The duplicate is gone; the canonical survives.
    assert get_entity_by_id(tmp_db, a.id) is None
    assert get_entity_by_id(tmp_db, b.id) is not None
