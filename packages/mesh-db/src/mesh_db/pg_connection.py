"""Postgres connection helpers for the knowledge store (Phase 12).

The knowledge schema lives in the same Postgres instance as the LangGraph
checkpoints + the `schedules` table (one store). Connection config is
env-driven and consistent with that existing connection:

* ``MESH_PG_URL`` — explicit knowledge-store DSN (preferred).
* ``LANGGRAPH_POSTGRES_URL`` — fallback; same database, the consolidation
  target. mesh-db reads it directly rather than importing mesh-a2a (which
  would invert the package dependency direction).

12d swaps the access layer onto these; 12b only uses them to apply migrations
and run the roles. No DuckDB import here.
"""
from __future__ import annotations

import os

import psycopg

MESH_PG_URL_ENV = "MESH_PG_URL"
LANGGRAPH_URL_ENV = "LANGGRAPH_POSTGRES_URL"


def pg_url(url: str | None = None) -> str:
    """Resolve the knowledge-store DSN, or raise if none is configured."""
    resolved = (
        url
        or os.environ.get(MESH_PG_URL_ENV)
        or os.environ.get(LANGGRAPH_URL_ENV)
        or ""
    ).strip()
    if not resolved:
        raise RuntimeError(
            f"{MESH_PG_URL_ENV} (or {LANGGRAPH_URL_ENV}) must be set for the "
            "Postgres knowledge store"
        )
    return resolved


def get_pg_connection(
    url: str | None = None, *, autocommit: bool = False
) -> psycopg.Connection:
    """Open a psycopg3 connection to the knowledge store."""
    return psycopg.connect(pg_url(url), autocommit=autocommit)
