"""Offline tests for the Postgres migration runner's statement splitter.

These need no live Postgres — they pin the SQL-splitting logic that broke once
on semicolons inside comments (Phase 12b). Live-Postgres schema verification is
covered by the 12b spike/verification harness, not the offline suite.
"""
from __future__ import annotations

from pathlib import Path

from mesh_db.pg_migrations import MIGRATIONS_DIR, _statements

PG_MIGRATIONS = Path(__file__).parent.parent / "packages" / "mesh-db" / "migrations_pg"


def test_statements_strips_comment_semicolons() -> None:
    sql = (
        "-- a comment with a ; semicolon inside it\n"
        "CREATE TABLE foo (id text);\n"
        "-- another; tricky comment\n"
        "CREATE TABLE bar (id text);\n"
    )
    stmts = _statements(sql)
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE TABLE foo")
    assert stmts[1].startswith("CREATE TABLE bar")
    # no leftover comment fragments leaked through as their own statements
    assert all("--" not in s for s in stmts)


def test_statements_drops_trailing_and_empty_chunks() -> None:
    assert _statements("") == []
    assert _statements("-- only a comment\n") == []
    assert _statements("CREATE TABLE x (id text);\n\n-- trailing\n") == [
        "CREATE TABLE x (id text)"
    ]


def test_real_migration_files_split_into_runnable_statements() -> None:
    # Every committed pg migration must split into non-empty statements that
    # start with a SQL keyword (no stray comment fragments).
    keywords = ("CREATE", "ALTER", "GRANT", "INSERT", "DROP", "REVOKE", "UPDATE")
    files = sorted(PG_MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"))
    assert files, "no pg migration files found"
    for f in files:
        stmts = _statements(f.read_text())
        assert stmts, f"{f.name} produced no statements"
        for s in stmts:
            assert s.upper().startswith(keywords), f"{f.name}: bad fragment {s[:40]!r}"


def test_migrations_dir_points_at_pg_migrations() -> None:
    assert MIGRATIONS_DIR.name == "migrations_pg"
    assert MIGRATIONS_DIR.is_dir()
