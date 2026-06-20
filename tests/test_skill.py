"""The Skill contract + registry (auction-free).

Defines a fake skill, exercises the registry, and runs the full vertical slice —
tension → run → Effect → write gateway → store — with no LLM, proving the
contract holds end to end. Skills no longer carry a ``bid``: the controller's
rule table (``mesh_agents.rules``) decides what to dispatch; a skill is just
``skill_id`` + ``handles`` + ``run``.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mesh_agents.skill import (
    Skill,
    all_skills,
    clear_registry,
    get_skill,
    register_skill,
    skills_for,
)
from mesh_db.beliefs import get_belief_by_id
from mesh_db.connection import MeshConnection
from mesh_db.effects import apply_effects
from mesh_models.belief import Belief
from mesh_models.effect import CreateBeliefEffect
from mesh_models.tension import Tension, TensionKind


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    clear_registry()
    yield
    clear_registry()


def _tension() -> Tension:
    return Tension(
        id="unextracted_source:abc",
        field_id="ai-robotics",
        kind=TensionKind.unextracted_source,
        subject="https://example.com/abc",
        rationale="unread",
        value=0.5,
        est_cost_usd=0.008,
        handler_skill="extract-source",
    )


def test_register_and_lookup_by_kind() -> None:
    @register_skill
    class _FakeExtract:
        skill_id = "fake-extract"
        handles = (TensionKind.unextracted_source,)

        async def run(self, conn: Any, tension: Tension, *, budget_usd: float) -> list[Any]:
            return []

    assert get_skill("fake-extract") is not None
    assert len(all_skills()) == 1
    matched = skills_for(TensionKind.unextracted_source)
    assert len(matched) == 1
    assert not skills_for(TensionKind.thin_belief)
    # Registered instance satisfies the runtime-checkable Protocol.
    assert isinstance(matched[0], Skill)


def test_duplicate_skill_id_rejected() -> None:
    @register_skill
    class _A:
        skill_id = "dup"
        handles = (TensionKind.thin_belief,)

        async def run(self, conn: Any, tension: Tension, *, budget_usd: float) -> list[Any]:
            return []

    with pytest.raises(ValueError, match="Duplicate skill_id"):

        @register_skill
        class _B:
            skill_id = "dup"
            handles = (TensionKind.stale_belief,)

            async def run(self, conn: Any, tension: Tension, *, budget_usd: float) -> list[Any]:
                return []


def test_full_slice_tension_to_store(tmp_db: MeshConnection) -> None:
    """A skill runs and returns an Effect; the gateway writes it. The skill itself
    never touches the DB."""

    @register_skill
    class _MakeBelief:
        skill_id = "make-belief"
        handles = (TensionKind.unextracted_source,)

        async def run(self, conn: Any, tension: Tension, *, budget_usd: float) -> list[Any]:
            return [
                CreateBeliefEffect(
                    field_id=tension.field_id,
                    belief=Belief(
                        topic="from-skill",
                        statement="a skill produced this via an effect",
                        confidence=0.5,
                        is_currently_held=True,
                    ),
                )
            ]

    skill = get_skill("make-belief")
    assert skill is not None
    tension = _tension()

    effects = asyncio.run(skill.run(tmp_db, tension, budget_usd=tension.est_cost_usd))
    report = apply_effects(tmp_db, effects)

    assert report.beliefs_created == 1
    belief_effect = effects[0]
    assert isinstance(belief_effect, CreateBeliefEffect)
    stored = get_belief_by_id(tmp_db, belief_effect.belief.id)
    assert stored is not None
    assert stored.statement == "a skill produced this via an effect"
