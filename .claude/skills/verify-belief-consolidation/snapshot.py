#!/usr/bin/env python
"""Snapshot live store state for before/after belief-consolidation verification.

Prints a JSON snapshot to stdout and writes it to the path given as the first
arg if provided. Read-only.

    uv run python .claude/skills/verify-belief-consolidation/snapshot.py [out.json]

Belief consolidation is strictly append-only (migration 011 grants no DELETE):
beliefs are never deleted (a merged-away belief is marked is_currently_held=false
but keeps its row + all revisions), revisions are only appended, and claims are
never touched. The captured counts make those guarantees checkable as deltas.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mesh_db import get_connection

SCALARS: dict[str, str] = {
    "beliefs": "SELECT count(*) FROM beliefs",
    "held_beliefs": "SELECT count(*) FROM beliefs WHERE is_currently_held",
    "belief_revisions": "SELECT count(*) FROM belief_revisions",
    "consolidator_revisions": "SELECT count(*) FROM belief_revisions "
    "WHERE revised_by_agent = 'belief_consolidator'",
    "claims": "SELECT count(*) FROM claims",
    "claim_ids_hash": "SELECT coalesce(md5(string_agg(id, ',' ORDER BY id)), '') FROM claims",
}


def snapshot() -> dict[str, Any]:
    conn = get_connection(read_only=True)
    try:
        values = {label: conn.execute(sql).fetchone()[0] for label, sql in SCALARS.items()}
    finally:
        conn.close()
    return {"captured_at": datetime.now(UTC).isoformat(), "values": values}


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
