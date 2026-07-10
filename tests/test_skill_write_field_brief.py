"""Tests for the ``write-field-brief`` skill and its overview/storage slice."""
from __future__ import annotations

import asyncio
from typing import Any

from mesh_agents.skills.write_field_brief import FieldBriefDraft, WriteFieldBriefSkill
from mesh_db.beliefs import create_belief
from mesh_db.connection import MeshConnection
from mesh_db.effects import apply_effects
from mesh_db.field_briefs import get_latest_field_brief
from mesh_db.overview import field_overview_inputs, movement_stats
from mesh_llm import LLMUsage
from mesh_models.belief import Belief
from mesh_models.effect import WriteFieldBriefEffect
from mesh_models.field import DEFAULT_FIELD_ID
from mesh_models.tension import Tension, TensionKind


class _FakeLLM:
    model = "fake-brief-model"

    def __init__(self, narrative: str = "The field currently believes X.") -> None:
        self._narrative = narrative
        self.calls: list[dict[str, Any]] = []

    def complete_with_usage(
        self, name: str, system: str, user: str, response_model: Any = None,
        options: Any = None,
    ) -> tuple[Any, int, LLMUsage]:
        self.calls.append({"system": system, "user": user})
        return (
            FieldBriefDraft(narrative=self._narrative),
            10,
            LLMUsage(model=self.model, input_tokens=100, output_tokens=50),
        )


def _tension() -> Tension:
    kind = TensionKind.stale_field_brief
    return Tension(
        id=f"{kind.value}:{DEFAULT_FIELD_ID}",
        field_id=DEFAULT_FIELD_ID,
        kind=kind,
        subject="field brief",
        rationale="due",
        value=0.2,
        est_cost_usd=0.02,
        handler_skill="write-field-brief",
        target_ref={"field_id": DEFAULT_FIELD_ID},
        signals={},
    )


def _seed_belief(conn: MeshConnection, topic: str, confidence: float) -> Belief:
    return create_belief(
        conn,
        Belief(topic=topic, statement=f"{topic} holds", confidence=confidence),
        field_id=DEFAULT_FIELD_ID,
    )


def test_empty_field_writes_nothing(tmp_db: MeshConnection) -> None:
    skill = WriteFieldBriefSkill(llm=_FakeLLM())  # type: ignore[arg-type]
    effects = asyncio.run(skill.run(tmp_db, _tension(), budget_usd=0.02))
    assert effects == []


def test_writes_brief_grounded_in_overview(tmp_db: MeshConnection) -> None:
    _seed_belief(tmp_db, "topic-a", 0.9)
    _seed_belief(tmp_db, "topic-b", 0.7)
    fake = _FakeLLM("Topic A dominates; topic B is emerging.")
    skill = WriteFieldBriefSkill(llm=fake)  # type: ignore[arg-type]

    effects = asyncio.run(skill.run(tmp_db, _tension(), budget_usd=0.02))
    assert len(effects) == 1
    eff = effects[0]
    assert isinstance(eff, WriteFieldBriefEffect)
    assert eff.narrative == "Topic A dominates; topic B is emerging."
    assert eff.model == "fake-brief-model"
    assert eff.inputs_summary["held_total"] == 2
    # The snapshot the LLM saw contains the seeded beliefs.
    assert "topic-a" in fake.calls[0]["user"]

    # Full slice: gateway persists; latest read returns it.
    report = apply_effects(tmp_db, effects)
    assert report.field_briefs_written == 1
    assert report.errors == []
    latest = get_latest_field_brief(tmp_db, DEFAULT_FIELD_ID)
    assert latest is not None
    assert latest.narrative == eff.narrative


def test_latest_brief_wins(tmp_db: MeshConnection) -> None:
    _seed_belief(tmp_db, "topic-a", 0.9)
    skill1 = WriteFieldBriefSkill(llm=_FakeLLM("first"))  # type: ignore[arg-type]
    skill2 = WriteFieldBriefSkill(llm=_FakeLLM("second"))  # type: ignore[arg-type]
    apply_effects(tmp_db, asyncio.run(skill1.run(tmp_db, _tension(), budget_usd=0.02)))
    apply_effects(tmp_db, asyncio.run(skill2.run(tmp_db, _tension(), budget_usd=0.02)))
    latest = get_latest_field_brief(tmp_db, DEFAULT_FIELD_ID)
    assert latest is not None
    assert latest.narrative == "second"


def test_overview_inputs_shape(tmp_db: MeshConnection) -> None:
    _seed_belief(tmp_db, "topic-a", 0.9)
    inputs = field_overview_inputs(tmp_db, DEFAULT_FIELD_ID)
    assert inputs["stats"]["held_total"] == 1
    assert inputs["strongest"][0]["topic"] == "topic-a"
    assert inputs["contested"] == []
    assert inputs["gaps"] == []
    stats = movement_stats(tmp_db, DEFAULT_FIELD_ID)
    assert stats["window_days"] == 7


def test_maintenance_tension_emitted_when_beliefs_exist(tmp_db: MeshConnection) -> None:
    from mesh_agents.agenda import maintenance_tensions

    assert not any(
        t.kind == TensionKind.stale_field_brief
        for t in maintenance_tensions(tmp_db, DEFAULT_FIELD_ID)
    )
    _seed_belief(tmp_db, "topic-a", 0.9)
    kinds = {t.kind for t in maintenance_tensions(tmp_db, DEFAULT_FIELD_ID)}
    assert TensionKind.stale_field_brief in kinds
