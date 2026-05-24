from __future__ import annotations

import os
from pathlib import Path

import duckdb


def get_db_path() -> Path:
    raw = os.environ.get("MESH_DB_PATH", "./data/mesh.db")
    path = Path(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_connection(
    db_path: Path | str | None = None,
    read_only: bool = False,
) -> duckdb.DuckDBPyConnection:
    path = Path(db_path) if db_path is not None else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path), read_only=read_only)
