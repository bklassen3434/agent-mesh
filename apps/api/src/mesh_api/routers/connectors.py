"""Connector catalog + per-field enablement endpoints (Phase 18 UX surface).

The read endpoints expose the global connector catalog (each connector's
``config_schema``) and one field's enablement + config so the wiki can render a
connector-configuration page. The write endpoint (``PUT``) is the per-field
enable/disable/reconfigure path — the second operational write in the otherwise
read-only API (the first being schedules). Config is validated against the
connector's ``config_schema`` at write time by ``enable_connector`` (a bad
config is a 422, never a mid-run failure). Connector content lives in the
Python registry; this router only toggles + parameterizes it per field.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from mesh_db.connection import MeshConnection, get_connection
from mesh_db.connectors import (
    enable_connector,
    get_connector,
    list_connectors,
    list_field_connectors,
)
from mesh_db.fields import get_field_by_slug
from mesh_models.connector import Connector, FieldConnector
from pydantic import BaseModel

from mesh_api.deps import ConnDep

router = APIRouter(prefix="/api/v1", tags=["connectors"])


class FieldConnectorUpdate(BaseModel):
    """Enable/disable + (re)configure a connector for a field."""

    config: dict[str, Any] | None = None
    enabled: bool = True


def get_writer_conn() -> Iterator[MeshConnection]:
    """Per-request writer connection (mesh_writer role) for the one connector
    write endpoint. Mirrors the schedules write surface: the API stays read-only
    for knowledge content, but operational config (schedules, connector
    enablement) is writable from the wiki."""
    conn = get_connection(read_only=False)
    try:
        yield conn
    finally:
        conn.close()


WriterConnDep = Annotated[MeshConnection, Depends(get_writer_conn)]


def _resolve_field_id(conn: MeshConnection, slug: str) -> str:
    field = get_field_by_slug(conn, slug)
    if field is None:
        raise HTTPException(status_code=404, detail=f"Unknown field '{slug}'")
    return field.id


@router.get(
    "/connectors",
    response_model=list[Connector],
    summary="List the connector catalog",
    description=(
        "The global catalog of source connectors a field can enable, each with "
        "its config_schema (the fields a field must supply to use it)."
    ),
)
def list_connectors_endpoint(conn: ConnDep) -> list[Connector]:
    return list_connectors(conn)


@router.get(
    "/fields/{slug}/connectors",
    response_model=list[FieldConnector],
    summary="List a field's connector enablement",
    description=(
        "The connectors configured for a field — enabled flag + per-field "
        "config. Join against /connectors by connector_id (== connector slug) "
        "for the catalog metadata."
    ),
)
def list_field_connectors_endpoint(slug: str, conn: ConnDep) -> list[FieldConnector]:
    field_id = _resolve_field_id(conn, slug)
    return list_field_connectors(conn, field_id)


@router.put(
    "/fields/{slug}/connectors/{connector_id}",
    response_model=FieldConnector,
    summary="Enable/disable or reconfigure a connector for a field",
    description=(
        "Upsert one field's enablement + config of a catalog connector. The "
        "config is validated against the connector's config_schema; an unknown "
        "key, wrong type, or missing required field is a 422. Persisted on the "
        "mesh_writer role; the next pipeline run dispatches the field's enabled "
        "connectors."
    ),
)
def put_field_connector(
    slug: str,
    connector_id: str,
    body: FieldConnectorUpdate,
    conn: WriterConnDep,
) -> FieldConnector:
    field_id = _resolve_field_id(conn, slug)
    if get_connector(conn, connector_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown connector '{connector_id}'")
    try:
        result = enable_connector(
            conn,
            field_id,
            connector_id,
            config=body.config or {},
            enabled=body.enabled,
        )
    except ValueError as exc:
        conn.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    conn.commit()
    return result
