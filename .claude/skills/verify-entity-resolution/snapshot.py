#!/usr/bin/env python
"""Snapshot live store state for before/after entity-reconcile verification.

Prints a JSON snapshot to stdout and writes it to the path given as the first
arg if provided. Read-only.

    uv run python .claude/skills/verify-entity-resolution/snapshot.py [out.json]

Captures the counts a merge can move (entities/relationships down as duplicates
are absorbed and colliding edges aggregated) plus a stable hash of the claim-id
set: a merge re-points claims.subject_entity_id but must never add or delete a
claim, so the claim count AND id-set must be identical before and after.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mesh_db import get_connection

SCALARS: dict[str, str] = {
    "entities": "SELECT count(*) FROM entities",
    "claims": "SELECT count(*) FROM claims",
    "relationships": "SELECT count(*) FROM relationships",
    "investigations": "SELECT count(*) FROM investigations",
    "null_name_embeddings": "SELECT count(*) FROM entities WHERE name_embedding IS NULL",
    # md5 over the sorted claim-id set: changes iff a claim is added or removed.
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
