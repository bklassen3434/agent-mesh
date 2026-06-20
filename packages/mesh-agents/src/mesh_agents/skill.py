"""Phase 1 of the agentic migration: the Skill contract + registry.

A **skill** is the unit of capability in the agentic mesh. It declares which
tension kinds it handles, *bids* on a tension (its own value/cost estimate), and
*runs* it — returning ``Effect``s, never writing. The market ranks bids and funds
the best under a budget; the write gateway applies the winners' effects.

This is the frozen contract for the Phase-2 fan-out: each skill is one class in
its own module with ``@register_skill``, so worktrees never edit a shared list
(the registration-conflict trap). To populate the registry, the skill modules
must be imported once at startup — ``load_builtin_skills`` does that import sweep.

Design parallels you already have:
* ``handles`` mirrors a connector's declared capabilities — dispatch by kind.
* ``bid`` is where the *value function* finally lives per-skill (Phase 0's
  central ``compute_agenda`` scorer is the temporary stand-in until skills own it).
* ``run`` returns ``list[Effect]`` so the decision/write split holds.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from mesh_models.tension import Tension, TensionKind
from pydantic import BaseModel, computed_field


class Bid(BaseModel):
    """A skill's offer to handle one tension: how much it's worth and what it'll
    cost. The market ranks by ``score`` (value per dollar) and funds top-down."""

    value: float
    est_cost_usd: float

    @computed_field  # type: ignore[prop-decorator]
    @property
    def score(self) -> float:
        return self.value / self.est_cost_usd if self.est_cost_usd > 0 else 0.0


@runtime_checkable
class Skill(Protocol):
    """One specialist capability. Stateless w.r.t. the board: it reads via ``conn``
    and returns intents. Implementations live in their own module + register via
    ``@register_skill``."""

    skill_id: str
    handles: tuple[TensionKind, ...]

    def bid(self, conn: Any, tension: Tension) -> Bid | None:
        """Offer to handle ``tension`` (value + cost), or ``None`` to decline.
        Cheap and read-only — called for every candidate every round."""
        ...

    async def run(self, conn: Any, tension: Tension, *, budget_usd: float) -> list[Any]:
        """Do the work and return ``Effect``s for the gateway. Writes nothing
        itself. ``budget_usd`` is what the market awarded this tension."""
        ...


# ── registry ─────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, Skill] = {}


def register_skill(skill_cls: type[Any]) -> type[Any]:
    """Class decorator: instantiate (no-arg) and register the skill by ``skill_id``.

    Use on a skill class in its own module::

        @register_skill
        class ExtractSourceSkill:
            skill_id = "extract-source"
            handles = (TensionKind.unextracted_source,)
            def bid(self, conn, tension): ...
            async def run(self, conn, tension, *, budget_usd): ...
    """
    instance = skill_cls()
    if not getattr(instance, "skill_id", None):
        raise ValueError(f"{skill_cls.__name__} must set a non-empty skill_id")
    if instance.skill_id in _REGISTRY:
        raise ValueError(f"Duplicate skill_id registered: {instance.skill_id}")
    _REGISTRY[instance.skill_id] = instance
    return skill_cls


def get_skill(skill_id: str) -> Skill | None:
    return _REGISTRY.get(skill_id)


def all_skills() -> list[Skill]:
    return list(_REGISTRY.values())


def skills_for(kind: TensionKind) -> list[Skill]:
    """Every registered skill that handles ``kind`` (may be more than one — the
    market lets them bid against each other)."""
    return [s for s in _REGISTRY.values() if kind in s.handles]


def clear_registry() -> None:
    """Test helper: drop all registrations."""
    _REGISTRY.clear()


def load_builtin_skills() -> list[Skill]:
    """Import the built-in skill modules so their ``@register_skill`` decorators
    run, then return the populated registry. Phase-2 worktrees add one import line
    here per skill (the *only* shared edit — append-only, conflict-trivial).

    No built-in skills exist yet (they land in Phase 2). Listed for shape::

        from mesh_agents.skills import extract_source  # noqa: F401
    """
    from mesh_agents.skills import (  # noqa: F401
        challenge_belief,
        dispatch_investigation,
        extract_source,
        investigate_gap,
        merge_candidate,
        scout_source,
        synthesize_belief,
    )

    return all_skills()
