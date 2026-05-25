from __future__ import annotations

from pathlib import Path

import duckdb
from mesh_db.migrations import apply_migrations


def _fresh_conn(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(tmp_path / "m.db"))


def test_migrations_apply_cleanly(tmp_path: Path) -> None:
    conn = _fresh_conn(tmp_path)
    apply_migrations(conn)
    tables = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
    expected = {"entities", "sources", "claims", "beliefs", "belief_revisions",
                "relationships", "investigations", "migrations", "pipeline_runs"}
    assert expected.issubset(tables)
    conn.close()


def test_migrations_idempotent(tmp_path: Path) -> None:
    conn = _fresh_conn(tmp_path)
    apply_migrations(conn)
    apply_migrations(conn)  # should not raise or duplicate
    row = conn.execute("SELECT COUNT(*) FROM migrations").fetchone()
    assert row is not None
    count = row[0]
    # 10 migration files
    assert count == 10
    conn.close()


def test_applied_at_recorded(tmp_path: Path) -> None:
    conn = _fresh_conn(tmp_path)
    apply_migrations(conn)
    rows = conn.execute("SELECT filename, applied_at FROM migrations ORDER BY filename").fetchall()
    assert len(rows) == 10
    for filename, applied_at in rows:
        assert filename.endswith(".sql")
        assert applied_at is not None
    conn.close()


def test_migration_order(tmp_path: Path) -> None:
    conn = _fresh_conn(tmp_path)
    apply_migrations(conn)
    rows = conn.execute("SELECT filename FROM migrations ORDER BY filename").fetchall()
    filenames = [r[0] for r in rows]
    assert filenames[0] == "001_create_entities.sql"
    assert filenames[-1] == "010_add_pipeline_run_type.sql"
    conn.close()
