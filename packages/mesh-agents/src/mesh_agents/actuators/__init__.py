"""Component actuators — the pluggable "how to fix each pipeline stage" layer.

Attribution files concerns against any pipeline stage; an actuator is what turns a
component's accumulated concerns into an auto-applied fix. Each actuator implements
one interface (draft a candidate → shadow-sample both arms on real data → promote
effects), so the generic ``improve-component`` and ``advance-experiment`` skills work
for every stage without stage-specific code. Registering an actuator is all it takes
to make a stage auto-fix.

Importing this package registers every built-in actuator (their modules call
``register_actuator`` at import).
"""
from __future__ import annotations

# Import side-effect: register the built-in actuators.
from mesh_agents.actuators import (  # noqa: F401
    challenge,
    confidence,
    decay,
    entity_resolution,
    extraction,
)
from mesh_agents.actuators.base import (
    Actuator,
    actuatable_components,
    get_actuator,
    register_actuator,
)

__all__ = [
    "Actuator",
    "actuatable_components",
    "get_actuator",
    "register_actuator",
]
