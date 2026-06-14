#!/usr/bin/env python
"""Assert a belief-consolidation pass stayed append-only and consistent.

Loads before/after snapshots (snapshot.py) taken around a
`mesh.cli consolidate-beliefs --apply` run and checks the headline Phase 19
guarantee — consolidation is **strictly append-only** — plus live structural
post-conditions:

  * No belief row deleted (a merged-away belief is marked not-held, never removed).
  * No revision row deleted (revisions only ever accumulate).
  * Claims untouched (consolidation never reads-modify-writes a claim).
  * Every merged-away belief is marked is_currently_held=false.
  * Confidence stays in [0, 1] after decay (floors at MESH_BELIEF_DECAY_FLOOR).
  * Investigation belief refs still resolve.

Writes a timestamped evidence report; exits non-zero on any FAIL.

    uv run python .claude/skills/verify-belief-consolidation/check_deltas.py \
        <before.json> <after.json>
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mesh_db import get_connection

# Live structural post-conditions: each SQL returns offending rows; PASS iff zero.
STRUCTURAL: dict[str, tuple[str, str]] = {
    "merged_beliefs_not_held": (
        "A belief with a 'merged into …' revision must be is_currently_held=false.",
        """
        SELECT DISTINCT b.id
        FROM beliefs b
        JOIN belief_revisions r ON r.belief_id = b.id
        WHERE r.rationale LIKE 'merged into %'
          AND b.is_currently_held = true
        """,
    ),
    "confidence_in_unit_range": (
        "Belief confidence must stay within [0, 1] after decay.",
        """
        SELECT id, confidence
        FROM beliefs
        WHERE confidence < 0 OR confidence > 1
        """,
    ),
    "investigation_opened_belief_refs_resolve": (
        "investigations.opened_by_belief_id must reference a real belief.",
        """
        SELECT i.id, i.opened_by_belief_id
        FROM investigations i
        WHERE i.opened_by_belief_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM beliefs b WHERE b.id = i.opened_by_belief_id)
        """,
    ),
    "investigation_resolution_belief_refs_resolve": (
        "investigations.resolution_belief_id must reference a real belief.",
        """
        SELECT i.id, i.resolution_belief_id
        FROM investigations i
        WHERE i.resolution_belief_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM beliefs b WHERE b.id = i.resolution_belief_id)
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

    # --- APPEND-ONLY delta checks (the headline guarantee) ---
    record(
        "beliefs_count_unchanged",
        av["beliefs"] == bv["beliefs"],
        f"beliefs {bv['beliefs']} -> {av['beliefs']} "
        "(never deleted, never created by consolidation)",
    )
    record(
        "belief_revisions_non_decreasing",
        av["belief_revisions"] >= bv["belief_revisions"],
        f"belief_revisions {bv['belief_revisions']} -> {av['belief_revisions']} "
        "(append-only; no revision deleted)",
    )
    record(
        "consolidator_revisions_non_decreasing",
        av["consolidator_revisions"] >= bv["consolidator_revisions"],
        f"belief_consolidator revisions {bv['consolidator_revisions']} -> "
        f"{av['consolidator_revisions']} (every change appends one)",
    )
    record(
        "held_beliefs_non_increasing",
        av["held_beliefs"] <= bv["held_beliefs"],
        f"held beliefs {bv['held_beliefs']} -> {av['held_beliefs']} "
        "(merge/archive only un-holds; nothing re-holds)",
    )
    record(
        "claims_count_unchanged",
        av["claims"] == bv["claims"],
        f"claims {bv['claims']} -> {av['claims']} (consolidation never touches claims)",
    )
    record(
        "claim_id_set_unchanged",
        av["claim_ids_hash"] == bv["claim_ids_hash"],
        "claim-id-set hash "
        + ("unchanged" if av["claim_ids_hash"] == bv["claim_ids_hash"] else "CHANGED"),
    )

    # If anything actually changed, at least one consolidator revision should have
    # been appended — guards against a no-op masquerading as success when held
    # beliefs dropped (a merge/archive without its mandatory revision).
    held_dropped = bv["held_beliefs"] - av["held_beliefs"]
    rev_added = av["consolidator_revisions"] - bv["consolidator_revisions"]
    record(
        "unheld_beliefs_have_revisions",
        held_dropped == 0 or rev_added >= held_dropped,
        f"{held_dropped} belief(s) un-held, {rev_added} consolidator revision(s) added",
    )

    # --- live STRUCTURAL post-conditions ---
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
    out = repo_root / ".evidence" / "verify-belief-consolidation" / ts
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
        f"# verify-belief-consolidation — {verdict}",
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
        print("usage: check_deltas.py <before.json> <after.json>", file=sys.stderr)
        return 2
    before, after = _load(sys.argv[1]), _load(sys.argv[2])
    assertions = check(before, after)
    out = write_evidence(before, after, assertions)

    failed = [a for a in assertions if not a["passed"]]
    verdict = "PASS" if not failed else "FAIL"
    passed_n = len(assertions) - len(failed)
    print(f"verify-belief-consolidation: {verdict}  ({passed_n}/{len(assertions)} passed)")
    for a in assertions:
        print(f"  [{'PASS' if a['passed'] else 'FAIL'}] {a['name']}: {a['detail']}")
    print(f"Evidence: {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
