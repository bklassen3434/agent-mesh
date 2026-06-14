#!/usr/bin/env python
"""Assert a skeptic-sweep run's store deltas match what the run reported.

Loads two snapshots from snapshot.py (before + after a `mesh-skeptic-sweep` run)
plus the skeptic_sweep run row recorded in the *after* snapshot, and checks the
observed store deltas line up with the reported counts. Writes a timestamped
evidence report; exits non-zero on any FAIL.

    uv run python .claude/skills/verify-skeptic/check_deltas.py <before.json> <after.json>

The skeptic applies an assessment by inserting one critique counter-claim, one
synthetic agent_reasoning source, and one belief_revision per applied assessment
(run accounting: claims_inserted / sources_inserted / beliefs_revised).
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _load(p: str) -> dict[str, Any]:
    return json.loads(Path(p).read_text())


def check(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    bc, ac = before["counts"], after["counts"]
    run = after.get("latest_skeptic_run")
    out: list[dict[str, Any]] = []

    def record(name: str, passed: bool, detail: str) -> None:
        out.append({"name": name, "passed": bool(passed), "detail": detail})

    # A new skeptic_sweep run must have been recorded.
    before_run = before.get("latest_skeptic_run")
    new_run = run is not None and (before_run is None or run["id"] != before_run["id"])
    record(
        "new_skeptic_run_recorded",
        new_run,
        f"after run id={run['id'] if run else None}, "
        f"before run id={before_run['id'] if before_run else None}",
    )
    if not run:
        return out

    def delta(label: str) -> int:
        return ac.get(label, 0) - bc.get(label, 0)

    # Reported delta vs observed store delta, side by side.
    pairs = [
        ("critique_claims_delta_matches_run", "critique_claims", "claims_inserted"),
        ("skeptic_revisions_delta_matches_run", "skeptic_revisions", "beliefs_revised"),
        ("skeptic_sources_delta_matches_run", "skeptic_sources", "sources_inserted"),
    ]
    for name, count_label, run_field in pairs:
        observed = delta(count_label)
        reported = int(run.get(run_field, 0))
        record(
            name,
            observed == reported,
            f"observed Δ{count_label}={observed} vs run.{run_field}={reported}",
        )

    # Append-only / monotonic counts (the sweep never deletes claims or revisions).
    for label in ("claims", "belief_revisions", "beliefs"):
        d = delta(label)
        record(f"{label}_monotonic", d >= 0, f"Δ{label}={d}")

    # Recorded errors must be well-formed (a partial failure that was recorded,
    # not a silent drop). Empty list passes; surface the count either way.
    errors = run.get("errors", []) or []
    malformed = [
        e
        for e in errors
        if not all(k in e for k in ("paper_id", "error_type", "error_message"))
    ]
    record(
        "run_errors_wellformed",
        not malformed,
        f"{len(errors)} error(s) recorded, {len(malformed)} malformed",
    )
    return out


def write_evidence(
    before: dict[str, Any], after: dict[str, Any], assertions: list[dict[str, Any]]
) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    repo_root = Path(__file__).resolve().parents[3]
    out = repo_root / ".evidence" / "verify-skeptic" / ts
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
        f"# verify-skeptic — {verdict}",
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
    print(f"verify-skeptic: {verdict}  ({len(assertions) - len(failed)}/{len(assertions)} passed)")
    for a in assertions:
        print(f"  [{'PASS' if a['passed'] else 'FAIL'}] {a['name']}: {a['detail']}")
    print(f"Evidence: {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
