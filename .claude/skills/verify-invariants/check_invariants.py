#!/usr/bin/env python
"""Evidence-capturing invariant checker for the Agent Mesh knowledge store.

Runs read-only assertions against the *live* Postgres knowledge store and writes
a timestamped evidence report (report.md + report.json) with an explicit verdict.
Exits non-zero if any invariant fails.

Run via the project venv so mesh_db / mesh_models resolve and the DB env is read:

    uv run python .claude/skills/verify-invariants/check_invariants.py

Connection comes from the same env the app uses (MESH_PG_READER_URL / MESH_PG_URL
/ LANGGRAPH_POSTGRES_URL); we open a read-only (mesh_reader) pooled connection.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mesh_db import get_connection
from mesh_models.claim import PREDICATE_TO_CLAIM_TYPE


@dataclass
class Assertion:
    name: str
    description: str
    # SQL must return one row per *violation*; PASS iff it returns zero rows.
    sql: str
    passed: bool = False
    count: int = 0
    samples: list[dict[str, Any]] = field(default_factory=list)


# Each query returns offending rows only. The connection sets
# search_path TO knowledge, public, so tables are referenced unqualified.
ASSERTIONS: list[Assertion] = [
    Assertion(
        name="claim_supersession_pointer",
        description="A claim marked 'superseded' must point at its successor; a "
        "claim must never supersede itself.",
        sql="""
            SELECT id, status, superseded_by_claim_id
            FROM claims
            WHERE (status = 'superseded' AND superseded_by_claim_id IS NULL)
               OR (superseded_by_claim_id = id)
        """,
    ),
    Assertion(
        name="revision_count_matches_rows",
        description="beliefs.revision_count must equal the number of append-only "
        "belief_revisions rows for that belief.",
        sql="""
            SELECT b.id AS belief_id,
                   b.revision_count AS recorded,
                   count(r.id) AS actual
            FROM beliefs b
            LEFT JOIN belief_revisions r ON r.belief_id = b.id
            GROUP BY b.id, b.revision_count
            HAVING b.revision_count <> count(r.id)
        """,
    ),
    Assertion(
        name="belief_supporting_claims_exist",
        description="Every id in beliefs.supporting_claim_ids must reference a "
        "real claim (array provenance is not FK-enforced; a merge/delete can "
        "leave a dangling ref).",
        sql="""
            SELECT b.id AS belief_id, cid AS missing_claim_id
            FROM beliefs b, unnest(b.supporting_claim_ids) AS cid
            WHERE NOT EXISTS (SELECT 1 FROM claims c WHERE c.id = cid)
        """,
    ),
    Assertion(
        name="belief_contradicting_claims_exist",
        description="Every id in beliefs.contradicting_claim_ids must reference a "
        "real claim.",
        sql="""
            SELECT b.id AS belief_id, cid AS missing_claim_id
            FROM beliefs b, unnest(b.contradicting_claim_ids) AS cid
            WHERE NOT EXISTS (SELECT 1 FROM claims c WHERE c.id = cid)
        """,
    ),
    Assertion(
        name="revision_trigger_claims_exist",
        description="Every id in belief_revisions.trigger_claim_ids must "
        "reference a real claim.",
        sql="""
            SELECT r.id AS revision_id, cid AS missing_claim_id
            FROM belief_revisions r, unnest(r.trigger_claim_ids) AS cid
            WHERE NOT EXISTS (SELECT 1 FROM claims c WHERE c.id = cid)
        """,
    ),
    Assertion(
        name="relationship_evidence_claims_exist",
        description="Every id in relationships.evidence_claim_ids must reference "
        "a real claim.",
        sql="""
            SELECT rel.id AS relationship_id, cid AS missing_claim_id
            FROM relationships rel, unnest(rel.evidence_claim_ids) AS cid
            WHERE NOT EXISTS (SELECT 1 FROM claims c WHERE c.id = cid)
        """,
    ),
    Assertion(
        name="no_self_relationships",
        description="After an entity merge, no relationship should collapse into "
        "a self-loop (from_entity_id = to_entity_id).",
        sql="""
            SELECT id, from_entity_id, type
            FROM relationships
            WHERE from_entity_id = to_entity_id
        """,
    ),
    Assertion(
        name="held_belief_has_support",
        description="A currently-held belief should rest on at least one "
        "supporting claim (an empty held belief is a synthesis bug).",
        sql="""
            SELECT id, topic
            FROM beliefs
            WHERE is_currently_held
              AND cardinality(coalesce(supporting_claim_ids, '{}')) = 0
        """,
    ),
]


def _claim_type_assertion() -> Assertion:
    """claim_type must be the deterministic 1:1 image of predicate.

    Built dynamically from the source-of-truth map so it can't drift from code.
    """
    pairs = ", ".join(
        f"('{pred}','{ct.value}')" for pred, ct in PREDICATE_TO_CLAIM_TYPE.items()
    )
    # Known predicate but wrong claim_type, OR unknown predicate not parked in
    # the inert 'speculative' bucket.
    sql = f"""
        WITH expected(predicate, claim_type) AS (VALUES {pairs})
        SELECT c.id, c.predicate, c.claim_type
        FROM claims c
        LEFT JOIN expected e ON e.predicate = c.predicate
        WHERE (e.claim_type IS NOT NULL AND c.claim_type <> e.claim_type)
           OR (e.claim_type IS NULL AND c.claim_type <> 'speculative')
    """
    return Assertion(
        name="claim_type_matches_predicate",
        description="claim_type is the deterministic image of predicate "
        "(PREDICATE_TO_CLAIM_TYPE); unknown predicates park in 'speculative'.",
        sql=sql,
    )


def run_assertions() -> list[Assertion]:
    assertions = [*ASSERTIONS, _claim_type_assertion()]
    conn = get_connection(read_only=True)
    try:
        for a in assertions:
            cur = conn.execute(a.sql)
            cols = [d.name for d in cur.description] if cur.description else []
            rows = cur.fetchall()
            a.count = len(rows)
            a.passed = a.count == 0
            a.samples = [dict(zip(cols, r, strict=False)) for r in rows[:5]]
    finally:
        conn.close()
    return assertions


def store_counts() -> dict[str, int]:
    """A small state snapshot recorded alongside the verdict for context."""
    conn = get_connection(read_only=True)
    counts: dict[str, int] = {}
    try:
        for tbl in (
            "entities",
            "sources",
            "claims",
            "beliefs",
            "belief_revisions",
            "relationships",
            "investigations",
        ):
            counts[tbl] = conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
    finally:
        conn.close()
    return counts


def write_evidence(assertions: list[Assertion], counts: dict[str, int]) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    repo_root = Path(__file__).resolve().parents[3]
    out = repo_root / ".evidence" / "verify-invariants" / ts
    out.mkdir(parents=True, exist_ok=True)

    verdict = "PASS" if all(a.passed for a in assertions) else "FAIL"
    payload = {
        "verdict": verdict,
        "captured_at": ts,
        "store_counts": counts,
        "assertions": [
            {
                "name": a.name,
                "description": a.description,
                "passed": a.passed,
                "violations": a.count,
                "samples": a.samples,
            }
            for a in assertions
        ],
    }
    (out / "report.json").write_text(json.dumps(payload, indent=2, default=str))

    lines = [
        f"# verify-invariants — {verdict}",
        "",
        f"Captured: {ts}",
        "",
        "## Store snapshot",
        "",
        "| table | rows |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in counts.items()],
        "",
        "## Assertions",
        "",
        "| invariant | result | violations |",
        "|---|---|---|",
    ]
    for a in assertions:
        mark = "✅ PASS" if a.passed else "❌ FAIL"
        lines.append(f"| {a.name} | {mark} | {a.count} |")
    for a in assertions:
        if not a.passed:
            lines += [
                "",
                f"### ❌ {a.name}",
                "",
                a.description,
                "",
                "Sample violations (≤5):",
                "",
                "```json",
                json.dumps(a.samples, indent=2, default=str),
                "```",
            ]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    return out


def main() -> int:
    assertions = run_assertions()
    counts = store_counts()
    out = write_evidence(assertions, counts)

    failed = [a for a in assertions if not a.passed]
    verdict = "PASS" if not failed else "FAIL"
    passed_n = len(assertions) - len(failed)
    print(f"verify-invariants: {verdict}  ({passed_n}/{len(assertions)} passed)")
    for a in assertions:
        mark = "PASS" if a.passed else "FAIL"
        print(f"  [{mark}] {a.name}: {a.count} violation(s)")
    print(f"Evidence: {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
