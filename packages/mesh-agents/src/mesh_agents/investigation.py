"""Shared types + helpers for the Phase 7a investigate skill.

Each scout adds an ``investigate_<source>`` skill that takes an
Investigation and runs a hypothesis-directed search against its source.
The output is a list of SourceRecord-shaped dicts so the coordinator can
hand them to claim_extractor exactly like normal scout output.

The empty-handler factory keeps less-developed scouts capability-
discoverable (they advertise the skill, return no results) without
demanding deep search implementations. Each scout's TODO is left in
its own module.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from mesh_a2a.card_builder import SkillSpec
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class InvestigateSkillInput(BaseModel):
    """Wire-shape input for any scout's investigate skill.

    Coordinator-driven — the field set is the same for every scout
    regardless of how that scout actually runs the search. Scouts pick
    whichever fields they can use (e.g. arxiv uses ``hypothesis`` as a
    keyword query, leaderboard uses ``target_entity_id`` to filter rows).
    """

    investigation_id: str
    hypothesis: str
    target_entity_id: str | None = None
    suggested_source_types: list[str] = Field(default_factory=list)
    max_results: int = 10


class InvestigateSkillOutput(BaseModel):
    """Wire-shape output. ``source_records`` are SourceRecord-shaped dicts
    (same fields as normal scout output) — the coordinator persists them
    via ``create_source`` and runs ``extract_claims`` on each."""

    investigation_id: str
    source_records: list[dict[str, Any]] = Field(default_factory=list)


def investigate_skill_spec(source_name: str) -> SkillSpec:
    """Build the ``investigate_<source>`` SkillSpec for an agent card.

    Centralized so all scouts advertise the same description shape; the
    coordinator picks one by ``suggested_source_types`` matching.
    """
    return SkillSpec(
        id=f"investigate_{source_name}",
        name=f"Investigate via {source_name}",
        description=(
            f"Run a hypothesis-directed search against {source_name} and "
            "return SourceRecords tagged with the investigation_id."
        ),
        tags=["investigation", source_name],
    )


def make_empty_investigate_handler(
    source_name: str,
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    """Stub handler for scouts whose investigate path is not yet deep.

    Returns an empty source_records list but logs the dispatch so the
    Langfuse trail makes it obvious the call landed but the source
    hasn't been hooked up yet.
    """

    async def _handler(payload: dict[str, Any]) -> dict[str, Any]:
        skill_input = InvestigateSkillInput.model_validate(payload)
        logger.info(
            "investigate_stub",
            extra={
                "source": source_name,
                "investigation_id": skill_input.investigation_id,
            },
        )
        return InvestigateSkillOutput(
            investigation_id=skill_input.investigation_id,
            source_records=[],
        ).model_dump(mode="json")

    return _handler
