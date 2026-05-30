"""Postgres knowledge-store connections (Phase 12d).

Replaces the DuckDB file connection with a pooled psycopg3 client while
preserving the access layer's call-site contract exactly: callers still do
``conn = get_connection(...)`` → use ``conn.execute(sql, params).fetchall()``
→ ``conn.close()``. ``close()`` returns the connection to its pool rather than
tearing down the socket, so pooling works transparently under the agents' and
API's concurrency.

Write-ownership (coordinator-owned writes) is preserved and now additionally
enforced by Postgres roles: ``read_only=True`` draws from the reader pool
(``mesh_reader``), otherwise the writer pool (``mesh_writer``). Each pool's DSN
is env-driven and falls back to the base owner DSN so single-URL/local/test
setups keep working:

* writes  → ``MESH_PG_WRITER_URL`` else base (``MESH_PG_URL`` /
  ``LANGGRAPH_POSTGRES_URL``)
* reads   → ``MESH_PG_READER_URL`` else base

Connections are autocommit, matching DuckDB's implicit-commit-per-statement
behavior (the access layer issues single-statement writes).
"""
from __future__ import annotations

import os
import threading
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from mesh_db.pg_connection import pg_url

WRITER_URL_ENV = "MESH_PG_WRITER_URL"
READER_URL_ENV = "MESH_PG_READER_URL"

_pools: dict[str, ConnectionPool] = {}
_pools_lock = threading.Lock()


def _writer_dsn() -> str:
    return (os.environ.get(WRITER_URL_ENV) or "").strip() or pg_url()


def _reader_dsn() -> str:
    return (os.environ.get(READER_URL_ENV) or "").strip() or pg_url()


def _configure(conn: psycopg.Connection[Any]) -> None:
    # Resolve the knowledge tables/views without qualifying every query, while
    # keeping `public` reachable for the schedules + LangGraph checkpoint tables.
    conn.execute("SET search_path TO knowledge, public")


def _pool_for(dsn: str) -> ConnectionPool:
    pool = _pools.get(dsn)
    if pool is None:
        with _pools_lock:
            pool = _pools.get(dsn)
            if pool is None:
                pool = ConnectionPool(
                    dsn,
                    min_size=1,
                    max_size=int(os.environ.get("MESH_PG_POOL_MAX", "10")),
                    kwargs={"autocommit": True},
                    configure=_configure,
                    open=True,
                )
                _pools[dsn] = pool
    return pool


class MeshConnection:
    """Thin proxy over a pooled psycopg connection.

    Exposes the subset of the connection interface the access layer uses
    (``execute``/``cursor``/``commit``/``rollback``) and routes ``close()``
    back to the owning pool. Usable as a context manager.
    """

    def __init__(self, pool: ConnectionPool, conn: psycopg.Connection[Any]) -> None:
        self._pool = pool
        self._conn: psycopg.Connection[Any] | None = conn

    @property
    def raw(self) -> psycopg.Connection[Any]:
        if self._conn is None:
            raise RuntimeError("connection already returned to the pool")
        return self._conn

    def execute(self, query: str, params: Any = None) -> psycopg.Cursor[Any]:
        return self.raw.execute(query, params)

    def cursor(self) -> psycopg.Cursor[Any]:
        return self.raw.cursor()

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        """Return the connection to the pool (idempotent)."""
        if self._conn is not None:
            self._pool.putconn(self._conn)
            self._conn = None

    def __enter__(self) -> MeshConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def get_connection(
    db_path: str | None = None,
    read_only: bool = False,
) -> MeshConnection:
    """Check out a pooled knowledge-store connection.

    ``db_path`` is accepted for signature compatibility with the old DuckDB
    layer and ignored — the store is Postgres now.
    """
    dsn = _reader_dsn() if read_only else _writer_dsn()
    pool = _pool_for(dsn)
    return MeshConnection(pool, pool.getconn())


def close_all_pools() -> None:
    """Close every open pool (test teardown / process shutdown)."""
    with _pools_lock:
        for pool in _pools.values():
            pool.close()
        _pools.clear()
