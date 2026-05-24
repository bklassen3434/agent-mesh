from __future__ import annotations

import os
from collections.abc import Iterator

import duckdb
from mesh_db.connection import get_connection


def get_conn() -> Iterator[duckdb.DuckDBPyConnection]:
    """Per-request read-only DuckDB connection.

    Why open-per-request: the coordinator is a short batch writer; opening fresh
    on each request means we always see committed writes without reconnect
    bookkeeping, and we never collide with the writer (DuckDB allows multiple
    concurrent readers in read-only mode).
    """
    conn = get_connection(read_only=True)
    try:
        yield conn
    finally:
        conn.close()


def db_exists() -> bool:
    """True if the configured DB path is present on disk.

    Used by /healthz to fail fast when the volume isn't mounted yet — important
    on a freshly cloned repo where no pipeline run has been kicked off.
    """
    raw = os.environ.get("MESH_DB_PATH", "./data/mesh.db")
    return os.path.exists(raw)
