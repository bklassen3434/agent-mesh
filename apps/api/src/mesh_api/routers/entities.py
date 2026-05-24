from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from mesh_db.claims import list_claims
from mesh_db.entities import count_entities, get_entity_by_id, list_entities
from mesh_db.relationships import list_relationships
from mesh_models.entity import Entity, EntityType

from mesh_api.deps import ConnDep
from mesh_api.schemas import EntityDetail, Page

router = APIRouter(prefix="/api/v1/entities", tags=["entities"])


@router.get(
    "",
    response_model=Page[Entity],
    summary="List entities",
    description=(
        "Paginated entity list with optional `type` and `q` (case-insensitive "
        "substring match on canonical_name)."
    ),
)
def list_entities_endpoint(
    conn: ConnDep,
    type: EntityType | None = None,
    q: str | None = Query(None, description="Substring match on canonical_name"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[Entity]:
    items = list_entities(conn, type=type, q=q, limit=limit, offset=offset)
    total = count_entities(conn, type=type, q=q)
    return Page[Entity](items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/{entity_id}",
    response_model=EntityDetail,
    summary="Entity detail",
    description=(
        "Entity record plus all claims where it is the subject and all "
        "relationships touching it (in either direction)."
    ),
)
def entity_detail(entity_id: str, conn: ConnDep) -> EntityDetail:
    entity = get_entity_by_id(conn, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    claims = list_claims(conn, entity_id=entity_id, limit=200)
    # Relationships in either direction — deduplicate by id.
    rels = {r.id: r for r in list_relationships(conn, from_entity_id=entity_id, limit=200)}
    for r in list_relationships(conn, to_entity_id=entity_id, limit=200):
        rels[r.id] = r
    return EntityDetail(entity=entity, claims=claims, relationships=list(rels.values()))
