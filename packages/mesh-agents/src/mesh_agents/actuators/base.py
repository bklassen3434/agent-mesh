"""The actuator interface + registry.

An :class:`Actuator` teaches the generic self-improvement skills how to fix ONE
pipeline stage. The three methods are all the skills need:

- ``draft`` — turn accumulated concerns into a candidate change (a ``treatment``
  config blob), or ``None`` to skip (e.g. it can't beat a cheap offline pre-filter).
- ``shadow_sample`` — run control vs treatment on real data and return each arm's
  scores. The treatment's output is graded but never persisted (shadow / quarantine).
- ``promote_effects`` — the effects to apply when the shadow A/B is won (install a
  prompt, write a config, …). The generic DecideExperiment + ResolveConcerns are
  added by the skill.

``draft`` and ``shadow_sample`` run on the controller's connection and may block on
LLM/DB — the skills call them off-thread. Registering an actuator (``register_actuator``)
is all it takes to make its stage auto-fix.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from mesh_models.improvement_concern import ImprovementConcern
from mesh_models.improvement_experiment import ExperimentArm, ImprovementExperiment


@runtime_checkable
class Actuator(Protocol):
    component: str  # the ConcernComponent value this fixes, e.g. "extraction"
    target: str  # the actuator handle recorded on the experiment, e.g. "extract-source"
    uses_llm: bool  # whether draft/shadow_sample may spend tokens (config actuators don't)

    def draft(
        self, conn: Any, field_id: str, concerns: list[ImprovementConcern]
    ) -> dict[str, Any] | None:
        """Build a candidate ``treatment`` from the concerns, or None to skip."""
        ...

    def shadow_sample(
        self, conn: Any, field_id: str, exp: ImprovementExperiment
    ) -> list[tuple[ExperimentArm, float]]:
        """Score control vs treatment on a batch of real inputs (output quarantined)."""
        ...

    def promote_effects(self, exp: ImprovementExperiment) -> list[Any]:
        """The effects that make the winning treatment live."""
        ...


_REGISTRY: dict[str, Actuator] = {}


def register_actuator(actuator_cls: type[Any]) -> type[Any]:
    """Class decorator: instantiate (no-arg) and register by ``component``."""
    inst = actuator_cls()
    if not getattr(inst, "component", ""):
        raise ValueError(f"{actuator_cls.__name__} must set a non-empty component")
    _REGISTRY[inst.component] = inst
    return actuator_cls


def get_actuator(component: str) -> Actuator | None:
    return _REGISTRY.get(component)


def actuatable_components() -> frozenset[str]:
    """Components with a wired actuator — the set the improvement producer lets
    auto-fire. Concerns for any other component still accumulate; they just wait for
    an actuator to be registered."""
    return frozenset(_REGISTRY)
