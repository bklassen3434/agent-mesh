#!/usr/bin/env python
"""Evidence-capturing checker for the routing (Phase 20) + discovery (Phase 22)
observability surfaces of the Agent Mesh store.

Two jobs in one read-only pass against the *live* Postgres store:

  * VERIFY — hard assertions that must hold (ledger integrity, valid investigation
    origins, discovery provenance). PASS = zero violations.
  * REPORT — informational context that has no single right answer but is worth
    capturing: the per-model tier split of LLM spend (cheap vs strong), null-model
    ledger rows, and discovery activity (investigations by origin + recent runs).

Writes a timestamped evidence report (report.md + report.json); exits non-zero
only if a *hard* assertion fails (the report context never flips the verdict).

    uv run python .claude/skills/verify-observability/check_observability.py

Connection comes from the same env the app uses (MESH_PG_READER_URL / MESH_PG_URL
/ LANGGRAPH_POSTGRES_URL); we open a read-only (mesh_reader) pooled connection.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mesh_db import get_connection

# Tier classification uses the same model ids the router defaults to (Phase 20).
CHEAP_MODEL = os.environ.get("MESH_ROUTE_CHEAP_MODEL", "claude-haiku-4-5")
STRONG_MODEL = os.environ.get("MESH_ROUTE_STRONG_MODEL", "claude-sonnet-4-6")
VALID_ORIGINS = ("curator", "skeptic", "discovery", "manual")


@dataclass
class Assertion:
    name: str
    description: str
    # SQL must return one row per *violation*; PASS iff it returns zero rows.
    sql: str
    passed: bool = False
    count: int = 0
    samples: list[dict[str, Any]] = field(default_factory=list)


# Hard assertions (PASS = zero violations). search_path is knowledge, public.
ASSERTIONS: list[Assertion] = [
    Assertion(
        name="llm_usage_tokens_non_negative",
        description="Token counts on the LLM ledger must never be negative.",
        sql="""
            SELECT id, model, input_tokens, output_tokens
            FROM llm_usage
            WHERE input_tokens < 0 OR output_tokens < 0
        """,
    ),
    Assertion(
        name="llm_usage_cost_non_negative",
        description="Estimated cost on the LLM ledger must never be negative.",
        sql="""
            SELECT id, model, estimated_cost_usd
            FROM llm_usage
            WHERE estimated_cost_usd < 0
        """,
    ),
    Assertion(
        name="llm_usage_run_id_references_run",
        description="Every llm_usage row must reference a real pipeline_runs row "
        "(the ledger joins to runs for per-run cost / field scoping).",
        sql="""
            SELECT u.id, u.run_id
            FROM llm_usage u
            WHERE NOT EXISTS (SELECT 1 FROM pipeline_runs r WHERE r.id = u.run_id)
        """,
    ),
    Assertion(
        name="investigation_origin_valid",
        description="investigations.origin must be one of "
        "curator|skeptic|discovery|manual (Phase 22a).",
        sql="""
            SELECT id, origin
            FROM investigations
            WHERE origin NOT IN ('curator', 'skeptic', 'discovery', 'manual')
        """,
    ),
    Assertion(
        name="discovery_investigation_has_rationale",
        description="Every discovery-opened investigation must carry a "
        "trigger_rationale (self-direction must be explainable).",
        sql="""
            SELECT id, question
            FROM investigations
            WHERE origin = 'discovery'
              AND (trigger_rationale IS NULL OR trigger_rationale = '')
        """,
    ),
]


def run_assertions(conn: Any) -> list[Assertion]:
    for a in ASSERTIONS:
        cur = conn.execute(a.sql)
        cols = [d.name for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        a.count = len(rows)
        a.passed = a.count == 0
        a.samples = [dict(zip(cols, r, strict=False)) for r in rows[:5]]
    return ASSERTIONS


def _tier(model: str | None) -> str:
    if model is None:
        return "unrecorded"
    if model == CHEAP_MODEL or "haiku" in model:
        return "cheap"
    if model == STRONG_MODEL or "sonnet" in model or "opus" in model:
        return "strong"
    return "other"


def gather_context(conn: Any) -> dict[str, Any]:
    """Informational report context — never affects the verdict."""
    # Per-model spend split, mapped to tiers.
    model_rows = conn.execute(
        """
        SELECT model, count(*) AS calls,
               coalesce(sum(input_tokens), 0) AS in_tok,
               coalesce(sum(output_tokens), 0) AS out_tok,
               coalesce(sum(estimated_cost_usd), 0) AS cost
        FROM llm_usage
        GROUP BY model
        ORDER BY cost DESC
        """
    ).fetchall()
    tier_split = [
        {
            "model": r[0],
            "tier": _tier(r[0]),
            "calls": r[1],
            "input_tokens": r[2],
            "output_tokens": r[3],
            "cost_usd": float(r[4]),
        }
        for r in model_rows
    ]
    null_model = conn.execute(
        "SELECT count(*) FROM llm_usage WHERE model IS NULL"
    ).fetchone()[0]

    # Discovery activity: investigations by origin, recent discovery runs.
    origin_rows = conn.execute(
        "SELECT origin, count(*) FROM investigations GROUP BY origin ORDER BY origin"
    ).fetchall()
    investigations_by_origin = {r[0]: r[1] for r in origin_rows}

    discovery_runs = conn.execute(
        """
        SELECT id, field_id, started_at, claims_inserted
        FROM pipeline_runs
        WHERE run_type = 'discovery'
        ORDER BY started_at DESC
        LIMIT 5
        """
    ).fetchall()
    recent_discovery_runs = [
        {
            "id": r[0],
            "field_id": r[1],
            "started_at": r[2],
            "claims_inserted": r[3],
        }
        for r in discovery_runs
    ]

    return {
        "cheap_model": CHEAP_MODEL,
        "strong_model": STRONG_MODEL,
        "tier_split": tier_split,
        "llm_usage_null_model_rows": null_model,
        "investigations_by_origin": investigations_by_origin,
        "recent_discovery_runs": recent_discovery_runs,
    }


def write_evidence(assertions: list[Assertion], ctx: dict[str, Any]) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    repo_root = Path(__file__).resolve().parents[3]
    out = repo_root / ".evidence" / "verify-observability" / ts
    out.mkdir(parents=True, exist_ok=True)

    verdict = "PASS" if all(a.passed for a in assertions) else "FAIL"
    payload = {
        "verdict": verdict,
        "captured_at": ts,
        "context": ctx,
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
        f"# verify-observability — {verdict}",
        "",
        f"Captured: {ts}",
        "",
        "## Hard assertions (PASS = zero violations)",
        "",
        "| assertion | result | violations |",
        "|---|---|---|",
    ]
    for a in assertions:
        mark = "✅ PASS" if a.passed else "❌ FAIL"
        lines.append(f"| {a.name} | {mark} | {a.count} |")

    lines += [
        "",
        "## Routing — LLM spend by model / tier (report only)",
        "",
        f"cheap = `{ctx['cheap_model']}`  ·  strong = `{ctx['strong_model']}`  ·  "
        f"null-model ledger rows = {ctx['llm_usage_null_model_rows']}",
        "",
        "| model | tier | calls | in tok | out tok | cost USD |",
        "|---|---|---|---|---|---|",
    ]
    for t in ctx["tier_split"]:
        lines.append(
            f"| {t['model']} | {t['tier']} | {t['calls']} | "
            f"{t['input_tokens']} | {t['output_tokens']} | {t['cost_usd']:.4f} |"
        )
    if not ctx["tier_split"]:
        lines.append("| _(no LLM usage recorded)_ | | | | | |")

    lines += [
        "",
        "## Discovery — investigations by origin (report only)",
        "",
        "| origin | count |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in ctx["investigations_by_origin"].items()],
    ]
    if not ctx["investigations_by_origin"]:
        lines.append("| _(no investigations)_ | |")
    lines += [
        "",
        f"Recent `discovery` runs: {len(ctx['recent_discovery_runs'])}",
    ]

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
    conn = get_connection(read_only=True)
    try:
        assertions = run_assertions(conn)
        ctx = gather_context(conn)
    finally:
        conn.close()
    out = write_evidence(assertions, ctx)

    failed = [a for a in assertions if not a.passed]
    verdict = "PASS" if not failed else "FAIL"
    passed_n = len(assertions) - len(failed)
    print(f"verify-observability: {verdict}  ({passed_n}/{len(assertions)} hard checks passed)")
    for a in assertions:
        mark = "PASS" if a.passed else "FAIL"
        print(f"  [{mark}] {a.name}: {a.count} violation(s)")
    print("  -- routing tiers:")
    for t in ctx["tier_split"]:
        print(
            f"     {t['tier']:>10} {t['model']}: {t['calls']} calls, "
            f"${t['cost_usd']:.4f}"
        )
    print(f"  -- investigations by origin: {ctx['investigations_by_origin']}")
    print(f"Evidence: {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
