from __future__ import annotations

import os
import time
from collections.abc import Generator

import psycopg
import pytest
from testcontainers.core.container import DockerContainer

# Knowledge tables truncated between tests for isolation (FK-safe via CASCADE).
_KNOWLEDGE_TABLES = [
    "entities", "sources", "claims", "beliefs", "belief_revisions",
    "relationships", "investigations", "pipeline_runs", "llm_usage",
    "processed_items", "agent_heuristic", "agent_heuristic_revision",
    "agent_invocations",
]


def _wait_ready(dsn: str, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=3) as c:
                c.execute("SELECT 1")
            return
        except Exception as e:
            last = e
            time.sleep(0.5)
    raise RuntimeError(f"Postgres not ready within {timeout}s: {last}")


@pytest.fixture(scope="session")
def _pg() -> Generator[str, None, None]:
    """Ephemeral pgvector/pg16 container for the whole test session.

    Starts once, applies the knowledge schema + roles via init_pg, and points
    the access layer at it through MESH_PG_URL. The writer/reader URLs are
    cleared so both pools resolve to this (superuser) DSN in tests — role
    enforcement is verified separately in the 12b harness.
    """
    container = (
        DockerContainer("pgvector/pgvector:pg16")
        .with_env("POSTGRES_USER", "test")
        .with_env("POSTGRES_PASSWORD", "test")
        .with_env("POSTGRES_DB", "test")
        .with_exposed_ports(5432)
    )
    container.start()
    try:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        dsn = f"postgresql://test:test@{host}:{port}/test"
        _wait_ready(dsn)

        os.environ["MESH_PG_URL"] = dsn
        os.environ.pop("MESH_PG_WRITER_URL", None)
        os.environ.pop("MESH_PG_READER_URL", None)
        os.environ.pop("LANGGRAPH_POSTGRES_URL", None)

        from mesh_db.pg_migrations import init_pg

        init_pg(dsn)
        yield dsn
    finally:
        from mesh_db.connection import close_all_pools

        close_all_pools()
        container.stop()


@pytest.fixture(autouse=True)
def _clean_knowledge(_pg: str) -> None:
    """Truncate all knowledge tables before each test for isolation."""
    with psycopg.connect(_pg, autocommit=True) as c:
        c.execute(
            "TRUNCATE "
            + ", ".join(f"knowledge.{t}" for t in _KNOWLEDGE_TABLES)
            + " RESTART IDENTITY CASCADE"
        )


@pytest.fixture
def tmp_db(_clean_knowledge: None, _pg: str) -> Generator[object, None, None]:
    """A pooled knowledge-store connection on a freshly truncated schema.

    Replaces the former temp-DuckDB fixture; the public interface (execute /
    fetchone / fetchall / close) is preserved so tests barely change.
    """
    from mesh_db.connection import get_connection

    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()
