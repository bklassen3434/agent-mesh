#!/usr/bin/env python
"""Snapshot live knowledge-store state for before/after pipeline verification.

Prints a JSON snapshot (store row counts + the latest pipeline run) to stdout,
and writes it to the path given as the first arg if provided. Read-only.

    uv run python .claude/skills/verify-pipeline/snapshot.py [out.json]
"""

from __future__ import annotations

import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mesh_db import get_connection
from mesh_db.pipeline_runs import list_pipeline_runs

TABLES = (
    "entities",
    "sources",
    "claims",
    "beliefs",
    "belief_revisions",
    "relationships",
    "investigations",
)


def _to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if hasattr(obj, "model_dump"):  # pydantic v2
        return obj.model_dump(mode="json")
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def snapshot() -> dict[str, Any]:
    conn = get_connection(read_only=True)
    try:
        counts = {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES
        }
        runs = list_pipeline_runs(conn, limit=1, run_type="pipeline")
    finally:
        conn.close()
    latest = _to_jsonable(runs[0]) if runs else None
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "latest_pipeline_run": latest,
    }


def main() -> int:
    snap = snapshot()
    text = json.dumps(snap, indent=2, default=str)
    if len(sys.argv) > 1:
        out = Path(sys.argv[1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
