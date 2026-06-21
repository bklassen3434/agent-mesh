"""The Skill contract + registry.

A **skill** is the unit of capability in the agentic mesh. It declares which
tension kinds it handles and *runs* one — returning ``Effect``s, never writing.
The deterministic **controller** (``apps/pipeline/controller.py``) decides *which*
tensions to dispatch and in what order via an explicit rule table
(``mesh_agents.rules``); the write gateway applies the resulting effects.

This used to be an auction: skills carried a ``bid()`` that returned a
value/cost ``Bid`` and the market funded the highest value-per-dollar offers
under a budget. The bidding is gone — selection and prioritisation are now
deterministic rules over board state, not an emergent price — so a skill is just
``skill_id`` + ``handles`` + ``run``. Routing stays a 1:1 map (``handler_skill``
/ the rule table name the skill for each kind); there was never more than one
skill per kind, so nothing is lost by dropping the bid-off.

Each skill is one class in its own module with ``@register_skill``, so worktrees
never edit a shared list (the registration-conflict trap). To populate the
registry, the skill modules must be imported once at startup —
``load_builtin_skills`` does that import sweep.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from mesh_models.tension import Tension, TensionKind


@runtime_checkable
class Skill(Protocol):
    """One specialist capability. Stateless w.r.t. the board: it reads via ``conn``
    and returns intents. Implementations live in their own module + register via
    ``@register_skill``."""

    skill_id: str
    handles: tuple[TensionKind, ...]

    async def run(self, conn: Any, tension: Tension, *, budget_usd: float) -> list[Any]:
        """Do the work and return ``Effect``s for the gateway. Writes nothing
        itself. ``budget_usd`` is an advisory per-tension spend hint the controller
        passes (the rough per-kind cost estimate); a skill may consult it but the
        controller no longer auctions a budget across skills."""
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
    """Every registered skill that handles ``kind``. The controller routes 1:1
    (one skill per kind), but the lookup stays a list for forward-compatibility."""
    return [s for s in _REGISTRY.values() if kind in s.handles]


def clear_registry() -> None:
    """Test helper: drop all registrations."""
    _REGISTRY.clear()


def load_builtin_skills() -> list[Skill]:
    """Import the built-in skill modules so their ``@register_skill`` decorators
    run, then return the populated registry. The one shared edit per new skill is
    appending an import here (append-only, conflict-trivial)."""
    from mesh_agents.skills import (  # noqa: F401
        adjudicate_contradiction,
        challenge_belief,
        consolidate_beliefs,
        consolidate_memory,
        dispatch_investigation,
        extract_source,
        investigate_gap,
        maintain_belief,
        merge_candidate,
        scout_source,
        synthesize_belief,
    )

    return all_skills()
