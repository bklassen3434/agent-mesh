#!/usr/bin/env python
"""Evidence-capturing field-isolation checker for the Agent Mesh knowledge store.

`field_id` is a *partition*, never a content axis (Phase 17): a row must never
reference a row in a different field. This runs read-only assertions against the
*live* Postgres knowledge store that look for any cross-field reference, and
writes a timestamped evidence report (report.md + report.json) with a verdict.
Exits non-zero if any field-isolation invariant fails.

    uv run python .claude/skills/verify-field-isolation/check_field_isolation.py

Connection comes from the same env the app uses (MESH_PG_READER_URL / MESH_PG_URL
/ LANGGRAPH_POSTGRES_URL); we open a read-only (mesh_reader) pooled connection.

Complements tests/test_field_isolation.py (which asserts resolution/memory never
cross fields at the application level) by checking the *data already on disk*.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mesh_db import get_connection


@dataclass
class Assertion:
    name: str
    description: str
    # SQL must return one row per *violation*; PASS iff it returns zero rows.
    sql: str
    passed: bool = False
    count: int = 0
    samples: list[dict[str, Any]] = field(default_factory=list)


# Each query returns offending rows only (a row whose field_id disagrees with a
# row it references). The connection sets search_path TO knowledge, public, so
# tables are referenced unqualified. belief_revisions and llm_usage carry no
# field_id of their own — they inherit it via their head FK (belief / run), so
# there is nothing to cross-check on them directly.
ASSERTIONS: list[Assertion] = [
    Assertion(
        name="claim_field_matches_subject_entity",
        description="A claim's field_id must equal its subject entity's field_id "
        "(claims.subject_entity_id -> entities.field_id).",
        sql="""
            SELECT c.id AS claim_id, c.field_id AS claim_field,
                   e.id AS entity_id, e.field_id AS entity_field
            FROM claims c
            JOIN entities e ON e.id = c.subject_entity_id
            WHERE c.field_id <> e.field_id
        """,
    ),
    Assertion(
        name="claim_field_matches_source",
        description="A claim's field_id must equal its source's field_id "
        "(claims.source_id -> sources.field_id).",
        sql="""
            SELECT c.id AS claim_id, c.field_id AS claim_field,
                   s.id AS source_id, s.field_id AS source_field
            FROM claims c
            JOIN sources s ON s.id = c.source_id
            WHERE c.field_id <> s.field_id
        """,
    ),
    Assertion(
        name="relationship_field_matches_endpoints",
        description="A relationship's field_id must equal both endpoint entities' "
        "field_id (from_entity_id and to_entity_id).",
        sql="""
            SELECT r.id AS relationship_id, r.field_id AS rel_field,
                   e1.field_id AS from_field, e2.field_id AS to_field
            FROM relationships r
            JOIN entities e1 ON e1.id = r.from_entity_id
            JOIN entities e2 ON e2.id = r.to_entity_id
            WHERE r.field_id <> e1.field_id OR r.field_id <> e2.field_id
        """,
    ),
    Assertion(
        name="relationship_evidence_claim_field_matches",
        description="Every claim in relationships.evidence_claim_ids must share "
        "the relationship's field_id.",
        sql="""
            SELECT r.id AS relationship_id, r.field_id AS rel_field,
                   cid AS claim_id, c.field_id AS claim_field
            FROM relationships r, unnest(r.evidence_claim_ids) AS cid
            JOIN claims c ON c.id = cid
            WHERE r.field_id <> c.field_id
        """,
    ),
    Assertion(
        name="belief_supporting_claim_field_matches",
        description="Every claim in beliefs.supporting_claim_ids must share the "
        "belief's field_id.",
        sql="""
            SELECT b.id AS belief_id, b.field_id AS belief_field,
                   cid AS claim_id, c.field_id AS claim_field
            FROM beliefs b, unnest(b.supporting_claim_ids) AS cid
            JOIN claims c ON c.id = cid
            WHERE b.field_id <> c.field_id
        """,
    ),
    Assertion(
        name="belief_contradicting_claim_field_matches",
        description="Every claim in beliefs.contradicting_claim_ids must share "
        "the belief's field_id.",
        sql="""
            SELECT b.id AS belief_id, b.field_id AS belief_field,
                   cid AS claim_id, c.field_id AS claim_field
            FROM beliefs b, unnest(b.contradicting_claim_ids) AS cid
            JOIN claims c ON c.id = cid
            WHERE b.field_id <> c.field_id
        """,
    ),
    Assertion(
        name="investigation_field_matches_target_entity",
        description="An investigation's field_id must equal its target entity's "
        "field_id when target_entity_id is set (not an FK, so check explicitly).",
        sql="""
            SELECT i.id AS investigation_id, i.field_id AS inv_field,
                   e.id AS entity_id, e.field_id AS entity_field
            FROM investigations i
            JOIN entities e ON e.id = i.target_entity_id
            WHERE i.target_entity_id IS NOT NULL
              AND i.field_id <> e.field_id
        """,
    ),
    Assertion(
        name="investigation_field_matches_related_entities",
        description="Every entity in investigations.related_entity_ids must share "
        "the investigation's field_id.",
        sql="""
            SELECT i.id AS investigation_id, i.field_id AS inv_field,
                   eid AS entity_id, e.field_id AS entity_field
            FROM investigations i, unnest(i.related_entity_ids) AS eid
            JOIN entities e ON e.id = eid
            WHERE i.field_id <> e.field_id
        """,
    ),
    Assertion(
        name="investigation_field_matches_opened_belief",
        description="An investigation's field_id must equal its opening belief's "
        "field_id when opened_by_belief_id is set.",
        sql="""
            SELECT i.id AS investigation_id, i.field_id AS inv_field,
                   b.id AS belief_id, b.field_id AS belief_field
            FROM investigations i
            JOIN beliefs b ON b.id = i.opened_by_belief_id
            WHERE i.opened_by_belief_id IS NOT NULL
              AND i.field_id <> b.field_id
        """,
    ),
    Assertion(
        name="investigation_field_matches_resolution_belief",
        description="An investigation's field_id must equal its resolution "
        "belief's field_id when resolution_belief_id is set.",
        sql="""
            SELECT i.id AS investigation_id, i.field_id AS inv_field,
                   b.id AS belief_id, b.field_id AS belief_field
            FROM investigations i
            JOIN beliefs b ON b.id = i.resolution_belief_id
            WHERE i.resolution_belief_id IS NOT NULL
              AND i.field_id <> b.field_id
        """,
    ),
    Assertion(
        name="all_field_ids_reference_a_real_field",
        description="Every field_id in use must reference a row in knowledge.fields "
        "(orphan partitions point at nothing).",
        sql="""
            SELECT 'claims' AS tbl, c.field_id
            FROM claims c
            WHERE NOT EXISTS (SELECT 1 FROM fields f WHERE f.id = c.field_id)
            UNION ALL
            SELECT 'entities', e.field_id
            FROM entities e
            WHERE NOT EXISTS (SELECT 1 FROM fields f WHERE f.id = e.field_id)
            UNION ALL
            SELECT 'beliefs', b.field_id
            FROM beliefs b
            WHERE NOT EXISTS (SELECT 1 FROM fields f WHERE f.id = b.field_id)
        """,
    ),
]


def run_assertions() -> list[Assertion]:
    conn = get_connection(read_only=True)
    try:
        for a in ASSERTIONS:
            cur = conn.execute(a.sql)
            cols = [d.name for d in cur.description] if cur.description else []
            rows = cur.fetchall()
            a.count = len(rows)
            a.passed = a.count == 0
            a.samples = [dict(zip(cols, r, strict=False)) for r in rows[:5]]
    finally:
        conn.close()
    return ASSERTIONS


def field_counts() -> list[dict[str, Any]]:
    """Per-field row counts for the partitioned tables, recorded as context."""
    tables = (
        "entities",
        "sources",
        "claims",
        "beliefs",
        "relationships",
        "investigations",
    )
    union = " UNION ALL ".join(
        f"SELECT '{t}' AS tbl, field_id, count(*) AS rows FROM {t} GROUP BY field_id"
        for t in tables
    )
    conn = get_connection(read_only=True)
    try:
        rows = conn.execute(
            f"SELECT tbl, field_id, rows FROM ({union}) q ORDER BY field_id, tbl"
        ).fetchall()
    finally:
        conn.close()
    return [{"table": r[0], "field_id": r[1], "rows": r[2]} for r in rows]


def write_evidence(assertions: list[Assertion], counts: list[dict[str, Any]]) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    repo_root = Path(__file__).resolve().parents[3]
    out = repo_root / ".evidence" / "verify-field-isolation" / ts
    out.mkdir(parents=True, exist_ok=True)

    verdict = "PASS" if all(a.passed for a in assertions) else "FAIL"
    payload = {
        "verdict": verdict,
        "captured_at": ts,
        "field_counts": counts,
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
        f"# verify-field-isolation — {verdict}",
        "",
        f"Captured: {ts}",
        "",
        "## Per-field row counts",
        "",
        "| table | field_id | rows |",
        "|---|---|---|",
        *[f"| {c['table']} | {c['field_id']} | {c['rows']} |" for c in counts],
        "",
        "## Assertions (PASS = no cross-field references)",
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
    counts = field_counts()
    out = write_evidence(assertions, counts)

    failed = [a for a in assertions if not a.passed]
    verdict = "PASS" if not failed else "FAIL"
    passed_n = len(assertions) - len(failed)
    print(f"verify-field-isolation: {verdict}  ({passed_n}/{len(assertions)} passed)")
    for a in assertions:
        mark = "PASS" if a.passed else "FAIL"
        print(f"  [{mark}] {a.name}: {a.count} violation(s)")
    print(f"Evidence: {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
