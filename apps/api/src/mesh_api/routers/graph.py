"""GET /api/v1/graph — knowledge graph nodes + edges in one shot.

Designed for the wiki's /graph route which renders a cytoscape view.
Returns every Entity as a node and every Relationship as an edge,
with enough metadata for client-side filtering by entity type.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from mesh_db.entities import count_entities, list_entities
from mesh_db.graph import NODE_CAP, graph_edges, graph_nodes
from mesh_db.relationships import list_relationships
from mesh_models.graph import GraphData, GraphDataEdge, GraphDataNode
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
    field: str = Query("ai-robotics", description="Field slug to scope results to"),
) -> GraphResponse:
    entities = list_entities(conn, limit=max_nodes, field_id=field)
    relationships = list_relationships(conn, limit=max_edges, field_id=field)
    entity_ids = {e.id for e in entities}
    # Edges referencing entities outside the bounded node set are dropped —
    # cytoscape complains on dangling edges and they're confusing in the UI.
    nodes = [
        GraphNode(id=e.id, label=e.canonical_name, type=e.type) for e in entities
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


@router.get(
    "/data",
    response_model=GraphData,
    summary="Pre-aggregated graph data for the redesigned /graph view",
    description=(
        "Node + edge lists with density baked in: node ``belief_count`` drives "
        f"radius, edge ``claim_count`` drives stroke width. Capped at the top "
        f"{NODE_CAP} entities by belief count; ``total_entities`` lets the UI "
        "show a 'showing N of M' notice. Edges are only included when both "
        "endpoints survive the cap."
    ),
)
def get_graph_data(
    conn: ConnDep,
    field: str = Query("ai-robotics", description="Field slug to scope results to"),
) -> GraphData:
    node_rows = graph_nodes(conn, limit=NODE_CAP, field_id=field)
    node_ids = {n["id"] for n in node_rows}
    edge_rows = graph_edges(conn, node_ids, field_id=field)
    return GraphData(
        nodes=[GraphDataNode(**n) for n in node_rows],
        edges=[GraphDataEdge(**e) for e in edge_rows],
        total_entities=count_entities(conn, field_id=field),
    )
