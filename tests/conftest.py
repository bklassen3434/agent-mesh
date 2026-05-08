from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import duckdb
import pytest
from mesh_db.migrations import apply_migrations


@pytest.fixture
def tmp_db(tmp_path: Path) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    db_path = tmp_path / "test.db"
    conn = duckdb.connect(str(db_path))
    apply_migrations(conn)
    yield conn
    conn.close()
