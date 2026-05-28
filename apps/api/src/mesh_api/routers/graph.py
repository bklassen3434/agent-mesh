"""GET /api/v1/graph — knowledge graph nodes + edges in one shot.

Designed for the wiki's /graph route which renders a cytoscape view.
Returns every Entity as a node and every Relationship as an edge,
with enough metadata for client-side filtering by entity type.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from mesh_db.entities import list_entities
from mesh_db.relationships import list_relationships
from pydantic import BaseModel

from mesh_api.deps import ConnDep

router = APIRouter(prefix="/api/v1/graph", tags=["graph"])


class GraphNode(BaseModel):
    id: str
    label: str
    type: str  # EntityType value


class GraphEdge(BaseModel):
    id: str
    source: str  # from_entity_id
    target: str  # to_entity_id
    type: str  # relationship type
    confidence: float


class GraphResponse(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


@router.get(
    "",
    response_model=GraphResponse,
    summary="Knowledge graph nodes + edges",
    description=(
        "All entities as nodes and all relationships as edges, in the shape a "
        "client-side graph library can consume directly. ``?max_nodes`` bounds "
        "the entity count for predictable browser performance on large meshes; "
        "the most recently created entities win."
    ),
)
def get_graph(
    conn: ConnDep,
    max_nodes: Annotated[int, Query(ge=1, le=5000)] = 500,
    max_edges: Annotated[int, Query(ge=1, le=20000)] = 2000,
) -> GraphResponse:
    entities = list_entities(conn, limit=max_nodes)
    relationships = list_relationships(conn, limit=max_edges)
    entity_ids = {e.id for e in entities}
    # Edges referencing entities outside the bounded node set are dropped —
    # cytoscape complains on dangling edges and they're confusing in the UI.
    nodes = [
        GraphNode(id=e.id, label=e.canonical_name, type=e.type.value) for e in entities
    ]
    edges = [
        GraphEdge(
            id=r.id,
            source=r.from_entity_id,
            target=r.to_entity_id,
            type=r.type,
            confidence=r.confidence,
        )
        for r in relationships
        if r.from_entity_id in entity_ids and r.to_entity_id in entity_ids
    ]
    return GraphResponse(nodes=nodes, edges=edges)
