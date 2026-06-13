"""Postgres migration runner for the knowledge store (Phase 12b).

Mirrors the DuckDB ``apply_migrations`` semantics — numbered ``NNN_*.sql``
files in ``migrations_pg/``, applied in filename order, only the unapplied
ones, each in its own transaction, tracked in a ``knowledge.migrations``
bookkeeping table — but targets Postgres via psycopg3 (the design locked in
docs/postgres-migration.md §5: extend the existing pattern, no Alembic).

Also owns role provisioning: ``ensure_roles`` creates the least-privilege
``mesh_writer`` / ``mesh_reader`` login roles (passwords env-driven) before
the grant migration (005) runs. Run as a superuser / DB owner (the
``langgraph`` container user qualifies) so CREATE EXTENSION / CREATE ROLE
succeed.

Entry point: ``python -m mesh_db.pg_migrations`` (or ``init_pg()``).
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg import sql

MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "migrations_pg"

WRITER_ROLE = "mesh_writer"
READER_ROLE = "mesh_reader"


def _statements(raw_sql: str) -> list[str]:
    """Split a migration file into individual statements.

    ``--`` line comments are stripped *before* splitting on ';', because our
    comments legitimately contain semicolons (e.g. "latent today; entity").
    Safe for these files: no statement has ``--`` inside a string literal and
    there are no dollar-quoted blocks. Empty chunks (e.g. the tail after the
    final ';') are dropped so psycopg never sees an empty command.
    """
    without_comments = "\n".join(
        line.split("--", 1)[0] for line in raw_sql.splitlines()
    )
    return [chunk.strip() for chunk in without_comments.split(";") if chunk.strip()]


def ensure_roles(conn: psycopg.Connection) -> None:
    """Create/refresh the writer + reader login roles. Idempotent.

    Passwords come from ``MESH_WRITER_PASSWORD`` / ``MESH_READER_PASSWORD``
    (container-friendly defaults). Roles are cluster-global; re-running only
    re-sets the password.
    """
    creds = (
        (WRITER_ROLE, os.environ.get("MESH_WRITER_PASSWORD", "mesh_writer")),
        (READER_ROLE, os.environ.get("MESH_READER_PASSWORD", "mesh_reader")),
    )
    with conn.transaction():
        for role, password in creds:
            exists = conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
            ).fetchone()
            verb = sql.SQL("ALTER ROLE") if exists else sql.SQL("CREATE ROLE")
            conn.execute(
                sql.SQL("{verb} {role} WITH LOGIN PASSWORD {pw}").format(
                    verb=verb,
                    role=sql.Identifier(role),
                    pw=sql.Literal(password),
                )
            )


def apply_pg_migrations(conn: psycopg.Connection) -> list[str]:
    """Apply unapplied migration files in order. Returns the files applied."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS knowledge")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge.migrations (
            filename   TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    conn.commit()

    applied: set[str] = {
        row[0]
        for row in conn.execute(
            "SELECT filename FROM knowledge.migrations"
        ).fetchall()
    }

    newly: list[str] = []
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name):
        if sql_file.name in applied:
            continue
        statements = _statements(sql_file.read_text())
        with conn.transaction():
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO knowledge.migrations (filename) VALUES (%s)",
                (sql_file.name,),
            )
        newly.append(sql_file.name)
    return newly


def init_pg(url: str | None = None) -> list[str]:
    """Stand up the knowledge schema on a clean (or existing) Postgres.

    Roles first (so the grant migration can reference them), then the
    numbered migrations. Idempotent end to end.
    """
    from mesh_db.connectors import seed_connectors
    from mesh_db.fields import seed_default_field
    from mesh_db.pg_connection import get_pg_connection

    with get_pg_connection(url) as conn:
        ensure_roles(conn)
        applied = apply_pg_migrations(conn)
        # Materialize the canonical ai-robotics FieldProfile + the built-in
        # connector catalog + the ai-robotics enablement from Python (the SQL
        # migrations only create empty tables). Idempotent upserts.
        seed_default_field(conn)
        seed_connectors(conn)
        return applied


def main() -> None:
    applied = init_pg()
    if applied:
        print(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("Knowledge schema already up to date.")


if __name__ == "__main__":
    main()
