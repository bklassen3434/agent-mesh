#!/usr/bin/env python
"""Assert an entity-reconcile pass merged duplicates without corrupting the store.

Two kinds of check in one pass:

  * DELTA — compares before/after snapshots (snapshot.py) taken around a
    `mesh.cli reconcile-entities --apply` run: entities and relationships only go
    down (duplicates absorbed, colliding edges aggregated), and crucially the
    claim set is untouched (a merge re-points claims.subject_entity_id but must
    never add or delete a claim).
  * STRUCTURAL — queries the live store for post-merge corruption that array /
    non-FK references can introduce: self-loop relationships and investigation
    entity references left dangling by a deleted duplicate.

Writes a timestamped evidence report; exits non-zero on any FAIL.

    uv run python .claude/skills/verify-entity-resolution/check_resolution.py \
        <before.json> <after.json>
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mesh_db import get_connection

# Structural post-conditions: each SQL returns offending rows; PASS iff zero.
# claims.subject_entity_id and relationships.from/to_entity_id are real FKs and
# cannot dangle; investigations.target_entity_id / related_entity_ids are plain
# columns (not FK-enforced), so a merge that deletes a duplicate can strand them.
STRUCTURAL: dict[str, tuple[str, str]] = {
    "no_self_relationships": (
        "A merge must delete self-loops it creates (from == to).",
        """
        SELECT id, from_entity_id, type
        FROM relationships
        WHERE from_entity_id = to_entity_id
        """,
    ),
    "no_dangling_investigation_target_entity": (
        "investigations.target_entity_id must reference a live entity after merge.",
        """
        SELECT i.id, i.target_entity_id
        FROM investigations i
        WHERE i.target_entity_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM entities e WHERE e.id = i.target_entity_id)
        """,
    ),
    "no_dangling_investigation_related_entities": (
        "Every investigations.related_entity_ids id must reference a live entity.",
        """
        SELECT i.id AS investigation_id, eid AS missing_entity_id
        FROM investigations i, unnest(i.related_entity_ids) AS eid
        WHERE NOT EXISTS (SELECT 1 FROM entities e WHERE e.id = eid)
        """,
    ),
}


def _load(p: str) -> dict[str, Any]:
    return json.loads(Path(p).read_text())


def check(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    bv, av = before["values"], after["values"]
    out: list[dict[str, Any]] = []

    def record(name: str, passed: bool, detail: str) -> None:
        out.append({"name": name, "passed": bool(passed), "detail": detail})

    # --- DELTA checks (before vs after) ---
    record(
        "entities_non_increasing",
        av["entities"] <= bv["entities"],
        f"entities {bv['entities']} -> {av['entities']}",
    )
    record(
        "relationships_non_increasing",
        av["relationships"] <= bv["relationships"],
        f"relationships {bv['relationships']} -> {av['relationships']}",
    )
    record(
        "claims_count_unchanged",
        av["claims"] == bv["claims"],
        f"claims {bv['claims']} -> {av['claims']} (a merge re-points, never deletes)",
    )
    record(
        "claim_id_set_unchanged",
        av["claim_ids_hash"] == bv["claim_ids_hash"],
        "claim-id-set hash "
        + ("unchanged" if av["claim_ids_hash"] == bv["claim_ids_hash"] else "CHANGED"),
    )
    record(
        "investigations_count_unchanged",
        av["investigations"] == bv["investigations"],
        f"investigations {bv['investigations']} -> {av['investigations']}",
    )
    record(
        "null_embeddings_non_increasing",
        av["null_name_embeddings"] <= bv["null_name_embeddings"],
        f"null name_embedding entities {bv['null_name_embeddings']} -> "
        f"{av['null_name_embeddings']} (reconcile backfills, never un-embeds)",
    )

    # --- STRUCTURAL checks (live store, post-apply) ---
    conn = get_connection(read_only=True)
    try:
        for name, (desc, sql) in STRUCTURAL.items():
            rows = conn.execute(sql).fetchall()
            record(name, len(rows) == 0, f"{desc} ({len(rows)} violation(s))")
    finally:
        conn.close()

    return out


def write_evidence(
    before: dict[str, Any], after: dict[str, Any], assertions: list[dict[str, Any]]
) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    repo_root = Path(__file__).resolve().parents[3]
    out = repo_root / ".evidence" / "verify-entity-resolution" / ts
    out.mkdir(parents=True, exist_ok=True)

    verdict = "PASS" if all(a["passed"] for a in assertions) else "FAIL"
    (out / "before.json").write_text(json.dumps(before, indent=2, default=str))
    (out / "after.json").write_text(json.dumps(after, indent=2, default=str))
    (out / "report.json").write_text(
        json.dumps(
            {"verdict": verdict, "captured_at": ts, "assertions": assertions},
            indent=2,
            default=str,
        )
    )

    lines = [
        f"# verify-entity-resolution — {verdict}",
        "",
        f"Captured: {ts}",
        "",
        "| assertion | result | detail |",
        "|---|---|---|",
    ]
    for a in assertions:
        mark = "✅ PASS" if a["passed"] else "❌ FAIL"
        lines.append(f"| {a['name']} | {mark} | {a['detail']} |")
    (out / "report.md").write_text("\n".join(lines) + "\n")
    return out


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: check_resolution.py <before.json> <after.json>", file=sys.stderr)
        return 2
    before, after = _load(sys.argv[1]), _load(sys.argv[2])
    assertions = check(before, after)
    out = write_evidence(before, after, assertions)

    failed = [a for a in assertions if not a["passed"]]
    verdict = "PASS" if not failed else "FAIL"
    passed_n = len(assertions) - len(failed)
    print(f"verify-entity-resolution: {verdict}  ({passed_n}/{len(assertions)} passed)")
    for a in assertions:
        print(f"  [{'PASS' if a['passed'] else 'FAIL'}] {a['name']}: {a['detail']}")
    print(f"Evidence: {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
