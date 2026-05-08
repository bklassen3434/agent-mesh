from __future__ import annotations

from pathlib import Path

import duckdb

MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "migrations"


def apply_migrations(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS migrations (
            filename VARCHAR PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    applied: set[str] = {
        row[0] for row in conn.execute("SELECT filename FROM migrations").fetchall()
    }

    sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)

    for sql_file in sql_files:
        if sql_file.name in applied:
            continue
        sql = sql_file.read_text()
        try:
            conn.execute("BEGIN")
            # Execute statements individually to handle INSTALL/LOAD directives
            for statement in _split_statements(sql):
                if statement:
                    conn.execute(statement)
            conn.execute(
                "INSERT INTO migrations (filename) VALUES (?)", [sql_file.name]
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _split_statements(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]
