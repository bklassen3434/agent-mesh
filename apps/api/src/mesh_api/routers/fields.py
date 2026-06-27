"""Field onboarding endpoints (Phase 18 UX surface).

A **Field** is the first-class scope that partitions all knowledge. These
endpoints let an operator create and manage fields (each carrying a stored
``FieldProfile`` that drives the extractor/skeptic/personalizer prompts) from
the wiki, instead of hand-writing rows. Reads are reader-role; create/patch are
the writer-role operational writes (like connector enablement). ``slug``/``id``
are immutable once created — they key every field-scoped row; a field is
deactivated, never deleted (no DELETE grant on ``knowledge.fields``).
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from mesh_db.fields import create_field, get_field_by_slug, list_fields, update_field
from mesh_models.field import Field, FieldProfile
from pydantic import BaseModel
from pydantic import Field as PField

from mesh_api.deps import ConnDep, WriterConnDep
from mesh_api.security import require_internal_admin

router = APIRouter(prefix="/api/v1/fields", tags=["fields"])


class FieldCreate(BaseModel):
    """Create a field. ``name`` is slugified into the immutable id/slug; the
    rest seed the field's prompt-driving FieldProfile."""

    name: str
    # A noun phrase the prompt builders template in, e.g.
    # "a materials-science research knowledge base".
    description: str
    entity_type_hints: list[str] = PField(default_factory=list)
    extraction_examples: str = ""
    topic_label: str = "sota"


class FieldPatch(BaseModel):
    """Patch a field's mutable attributes. All optional; slug/id never change."""

    name: str | None = None
    description: str | None = None
    entity_type_hints: list[str] | None = None
    extraction_examples: str | None = None
    topic_label: str | None = None
    is_active: bool | None = None


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug


@router.get(
    "",
    response_model=list[Field],
    summary="List fields",
    description="Every field scope, each with its stored FieldProfile.",
)
def list_fields_endpoint(conn: ConnDep, active_only: bool = False) -> list[Field]:
    return list_fields(conn, active_only=active_only)


@router.get(
    "/{slug}",
    response_model=Field,
    summary="Get a field",
)
def get_field_endpoint(slug: str, conn: ConnDep) -> Field:
    field = get_field_by_slug(conn, slug)
    if field is None:
        raise HTTPException(status_code=404, detail=f"Unknown field '{slug}'")
    return field


@router.post(
    "",
    response_model=Field,
    status_code=201,
    summary="Create a field",
    description=(
        "Create a new field scope from a name + profile. The name is slugified "
        "into the immutable id/slug. A field starts with no connectors enabled — "
        "enable sources for it via the connectors endpoints."
    ),
    dependencies=[Depends(require_internal_admin)],
)
def create_field_endpoint(body: FieldCreate, conn: WriterConnDep) -> Field:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name must not be empty")
    if not body.description.strip():
        raise HTTPException(status_code=422, detail="description must not be empty")
    slug = _slugify(name)
    if not slug:
        raise HTTPException(
            status_code=422, detail="name must contain at least one alphanumeric character"
        )
    if get_field_by_slug(conn, slug) is not None:
        raise HTTPException(status_code=409, detail=f"Field '{slug}' already exists")
    field = Field(
        id=slug,
        name=name,
        slug=slug,
        profile=FieldProfile(
            slug=slug,
            name=name,
            description=body.description.strip(),
            entity_type_hints=body.entity_type_hints,
            extraction_examples=body.extraction_examples,
            topic_label=body.topic_label or "sota",
        ),
    )
    create_field(conn, field)
    conn.commit()
    return field


@router.patch(
    "/{slug}",
    response_model=Field,
    summary="Update a field",
    description=(
        "Patch a field's name, profile fields, and/or active flag. slug/id are "
        "immutable. Profile edits take effect on the next pipeline run."
    ),
    dependencies=[Depends(require_internal_admin)],
)
def patch_field_endpoint(slug: str, body: FieldPatch, conn: WriterConnDep) -> Field:
    existing = get_field_by_slug(conn, slug)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Unknown field '{slug}'")

    name = existing.name
    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(status_code=422, detail="name must not be empty")
        name = body.name.strip()

    p = existing.profile
    description = p.description
    if body.description is not None:
        if not body.description.strip():
            raise HTTPException(status_code=422, detail="description must not be empty")
        description = body.description.strip()

    new_profile = FieldProfile(
        slug=p.slug,
        name=name,
        description=description,
        entity_type_hints=(
            body.entity_type_hints if body.entity_type_hints is not None else p.entity_type_hints
        ),
        extraction_examples=(
            body.extraction_examples
            if body.extraction_examples is not None
            else p.extraction_examples
        ),
        topic_label=body.topic_label if body.topic_label is not None else p.topic_label,
    )
    updated = update_field(
        conn, existing.id, name=name, profile=new_profile, is_active=body.is_active
    )
    conn.commit()
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Unknown field '{slug}'")
    return updated
