"""Built-in market skills (Phase 2b of the agentic migration).

Each module here defines exactly one skill class registered via
``@register_skill`` (see ``mesh_agents.skill``). The market imports them through
``load_builtin_skills`` so their decorators run and populate the registry; this
package never holds a central skill *list* to fight over across worktrees.
"""
from __future__ import annotations
