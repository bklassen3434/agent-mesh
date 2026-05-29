"""Pre-aggregated knowledge-graph view models (Phase 9).

Backs the wiki's redesigned /graph route. The node/edge lists are
aggregated server-side (belief counts, claim counts) so the browser
renders a fixed, bounded payload instead of computing density itself.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class GraphDataNode(BaseModel):
    id: str
    label: str
    type: str  # EntityType value
    belief_count: int
    last_claim_at: datetime | None = None


class GraphDataEdge(BaseModel):
    source: str  # from_entity_id
    target: str  # to_entity_id
    relationship_type: str
    claim_count: int


class GraphData(BaseModel):
    """Bounded graph payload. ``total_entities`` lets the UI show a
    "showing N of M" notice when the node list is capped."""

    nodes: list[GraphDataNode]
    edges: list[GraphDataEdge]
    total_entities: int
