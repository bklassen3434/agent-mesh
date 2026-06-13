"""The SourceConnector protocol (Phase 17c).

Formalizes the de-facto interface every scout already implements: given a
per-field **config** (search terms — categories / keywords / topics / …), a
``max_results`` cap and an optional ``since`` window, produce a list of scouted
source records (each a ``Source`` + payload as a JSON dict, the shape the
coordinator's ingest path already consumes). Each scout's A2A skill handler
(``scout_<slug>``) is a conforming connector; the catalog definition + per-field
config live in ``mesh_models.connector`` / ``knowledge.{connectors,
field_connectors}``.

This phase ships built-in connectors only (the existing scouts). The self-serve,
config-driven connector layer (web_search / rss / rest_json) is Phase 18; it will
implement this same protocol.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SourceConnector(Protocol):
    """What every source connector does. A scout skill handler conforms by taking
    a config-bearing payload and returning ``{"papers": [...]}``."""

    def scout(
        self,
        *,
        config: dict[str, Any],
        max_results: int,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """Discover source records for a field, using its per-field ``config``."""
        ...


def connector_skill_id(slug: str) -> str:
    """The A2A scout skill that runs a connector (``scout_<slug>``)."""
    return f"scout_{slug}"
