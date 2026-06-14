from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from mesh_db.connection import MeshConnection, get_connection

__all__ = ["ConnDep", "WriterConnDep", "db_exists", "get_conn", "get_writer_conn"]


def get_conn() -> Iterator[MeshConnection]:
    """Per-request read-only Postgres connection (from the reader pool).

    Why open-per-request: the coordinator is the single batch writer; drawing a
    fresh pooled connection per request means we always see committed writes,
    and the read-only role (mesh_reader) enforces that the API never writes.
    """
    conn = get_connection(read_only=True)
    try:
        yield conn
    finally:
        conn.close()


ConnDep = Annotated[MeshConnection, Depends(get_conn)]


def get_writer_conn() -> Iterator[MeshConnection]:
    """Per-request writer connection (mesh_writer role).

    Used by the handful of operational-config write endpoints (schedules,
    connector enablement, field onboarding). The API stays read-only for
    knowledge content; only config rows are writable from the wiki. The caller
    owns the transaction (commit on success, rollback on validation failure).
    """
    conn = get_connection(read_only=False)
    try:
        yield conn
    finally:
        conn.close()


WriterConnDep = Annotated[MeshConnection, Depends(get_writer_conn)]


def db_exists() -> bool:
    """True if the knowledge store is reachable and migrated.

    Used by /healthz to fail fast when Postgres isn't up or the schema hasn't
    been applied yet — the Postgres equivalent of the old "is the DuckDB volume
    mounted?" check. Best-effort: never raises.
    """
    try:
        conn = get_connection(read_only=True)
        try:
            conn.execute("SELECT 1 FROM knowledge.beliefs LIMIT 1").fetchone()
            return True
        finally:
            conn.close()
    except Exception:
        return False
