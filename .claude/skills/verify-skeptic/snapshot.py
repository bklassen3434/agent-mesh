#!/usr/bin/env python
"""Snapshot live store state for before/after skeptic-sweep verification.

Prints a JSON snapshot (skeptic-relevant row counts + the latest skeptic_sweep
run) to stdout, and writes it to the path given as the first arg if provided.
Read-only.

    uv run python .claude/skills/verify-skeptic/snapshot.py [out.json]

Counts are store-wide (the sweep is single-field, but belief_revisions carry no
field_id; with no concurrent writer a single-field sweep's delta == the global
delta — the same assumption /verify-pipeline makes).
"""

from __future__ import annotations

import dataclasses
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mesh_db import get_connection
from mesh_db.pipeline_runs import list_pipeline_runs

# label -> SQL returning a single count. The skeptic writes critique claims
# (claim_type='critique', extracted_by_agent='skeptic'), synthetic
# agent_reasoning sources, and belief_revisions attributed to 'skeptic'.
COUNTS: dict[str, str] = {
    "claims": "SELECT count(*) FROM claims",
    "critique_claims": "SELECT count(*) FROM claims WHERE claim_type = 'critique'",
    "beliefs": "SELECT count(*) FROM beliefs",
    "belief_revisions": "SELECT count(*) FROM belief_revisions",
    "skeptic_revisions": "SELECT count(*) FROM belief_revisions "
    "WHERE revised_by_agent = 'skeptic'",
    "skeptic_sources": "SELECT count(*) FROM sources "
    "WHERE type = 'agent_reasoning' AND author = 'skeptic'",
    "investigations": "SELECT count(*) FROM investigations",
}


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
        counts = {label: conn.execute(sql).fetchone()[0] for label, sql in COUNTS.items()}
        runs = list_pipeline_runs(conn, limit=1, run_type="skeptic_sweep")
    finally:
        conn.close()
    latest = _to_jsonable(runs[0]) if runs else None
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "counts": counts,
        "latest_skeptic_run": latest,
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
