#!/usr/bin/env python
"""Evidence-capturing verifier for the read API (apps/api, :8000).

Captures live responses from the running API, then asserts internal consistency
across them, and writes a timestamped evidence report. Read-only; stdlib only.

    uv run python .claude/skills/verify-api/check_api.py
    API_BASE=http://localhost:8000 uv run python .claude/skills/verify-api/check_api.py

Exits non-zero if any assertion fails or the API is unreachable.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE = os.environ.get("API_BASE", "http://localhost:8000").rstrip("/")
TIMEOUT = float(os.environ.get("API_TIMEOUT", "15"))


def get(path: str) -> tuple[int, Any]:
    url = f"{BASE}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
            ctype = resp.headers.get("Content-Type", "")
            parsed = json.loads(body) if "json" in ctype else body
            return resp.status, parsed
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return 0, str(e.reason)


def _items(page: Any) -> list[Any]:
    """Page[...] envelopes vary; pull the list out defensively."""
    if isinstance(page, list):
        return page
    if isinstance(page, dict):
        for key in ("items", "results", "data"):
            if isinstance(page.get(key), list):
                return page[key]
    return []


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    repo_root = Path(__file__).resolve().parents[3]
    out = repo_root / ".evidence" / "verify-api" / ts
    out.mkdir(parents=True, exist_ok=True)

    captures: dict[str, Any] = {}
    assertions: list[dict[str, Any]] = []

    def record(name: str, passed: bool, detail: str) -> None:
        assertions.append({"name": name, "passed": bool(passed), "detail": detail})

    # --- capture (write raw responses first, so failures keep their evidence) ---
    endpoints = {
        "healthz": "/healthz",
        "stats": "/api/v1/stats",
        "beliefs": "/api/v1/beliefs?limit=10",
        "claims": "/api/v1/claims?limit=10",
        "graph": "/api/v1/graph?max_nodes=200&max_edges=400",
    }
    for key, path in endpoints.items():
        status, body = get(path)
        captures[key] = {"path": path, "status": status, "body": body}
        (out / f"{key}.json").write_text(json.dumps(captures[key], indent=2, default=str))

    # --- assertions ---
    # 1. API reachable + healthz ok
    hz = captures["healthz"]
    reachable = hz["status"] == 200
    record("api_reachable", reachable, f"GET /healthz -> {hz['status']}")
    if not reachable:
        # Nothing else is meaningful if the service is down.
        _finalize(out, ts, captures, assertions)
        return 1
    hz_ok = isinstance(hz["body"], dict) and hz["body"].get("status") == "ok"
    record("healthz_status_ok", hz_ok, f"healthz body: {hz['body']}")

    # 2. stats present with non-negative counts
    stats = captures["stats"]
    stats_ok = stats["status"] == 200 and isinstance(stats["body"], dict)
    record("stats_available", stats_ok, f"GET /api/v1/stats -> {stats['status']}")
    if stats_ok:
        negs = {
            k: v
            for k, v in stats["body"].items()
            if isinstance(v, int) and v < 0
        }
        record("stats_counts_non_negative", not negs, f"negative counts: {negs or 'none'}")

    # 3. graph edges reference nodes that exist in the same payload
    graph = captures["graph"]
    if graph["status"] == 200 and isinstance(graph["body"], dict):
        nodes = {n.get("id") for n in graph["body"].get("nodes", [])}
        edges = graph["body"].get("edges", [])
        dangling = [
            e
            for e in edges
            if e.get("source") not in nodes or e.get("target") not in nodes
        ]
        record(
            "graph_edges_reference_real_nodes",
            not dangling,
            f"{len(edges)} edges, {len(nodes)} nodes, {len(dangling)} dangling",
        )
    else:
        record("graph_edges_reference_real_nodes", False, f"graph -> {graph['status']}")

    # 4. pagination total is consistent (total >= returned page length)
    for key in ("beliefs", "claims"):
        cap = captures[key]
        if cap["status"] != 200:
            record(f"{key}_page_ok", False, f"{cap['path']} -> {cap['status']}")
            continue
        items = _items(cap["body"])
        total = cap["body"].get("total") if isinstance(cap["body"], dict) else None
        ok = total is None or (isinstance(total, int) and total >= len(items))
        record(f"{key}_page_total_sane", ok, f"total={total}, returned={len(items)}")

    # 5. a sampled belief detail resolves its own supporting claims
    beliefs = _items(captures["beliefs"]["body"])
    if beliefs:
        bid = beliefs[0].get("id")
        status, detail = get(f"/api/v1/beliefs/{bid}")
        captures["belief_detail"] = {"path": f"/api/v1/beliefs/{bid}", "status": status, "body": detail}
        (out / "belief_detail.json").write_text(json.dumps(captures["belief_detail"], indent=2, default=str))
        if status == 200 and isinstance(detail, dict):
            support_ids = set(detail.get("supporting_claim_ids", []) or [])
            resolved = {c.get("id") for c in detail.get("supporting_claims", []) or []}
            # Every resolved claim must be one this belief actually cites.
            stray = resolved - support_ids if support_ids else set()
            record(
                "belief_detail_claims_consistent",
                not stray,
                f"belief {bid}: {len(support_ids)} cited, {len(resolved)} resolved",
            )
        else:
            record("belief_detail_claims_consistent", False, f"belief detail -> {status}")
    else:
        record("belief_detail_claims_consistent", True, "no beliefs to sample (vacuously ok)")

    return _finalize(out, ts, captures, assertions)


def _finalize(out: Path, ts: str, captures: dict, assertions: list[dict]) -> int:
    failed = [a for a in assertions if not a["passed"]]
    verdict = "PASS" if not failed else "FAIL"
    payload = {
        "verdict": verdict,
        "captured_at": ts,
        "api_base": BASE,
        "assertions": assertions,
    }
    (out / "report.json").write_text(json.dumps(payload, indent=2, default=str))

    lines = [
        f"# verify-api — {verdict}",
        "",
        f"Captured: {ts}  ·  API: {BASE}",
        "",
        "| assertion | result | detail |",
        "|---|---|---|",
    ]
    for a in assertions:
        mark = "✅ PASS" if a["passed"] else "❌ FAIL"
        lines.append(f"| {a['name']} | {mark} | {a['detail']} |")
    (out / "report.md").write_text("\n".join(lines) + "\n")

    print(f"verify-api: {verdict}  ({len(assertions) - len(failed)}/{len(assertions)} passed)")
    for a in assertions:
        print(f"  [{'PASS' if a['passed'] else 'FAIL'}] {a['name']}: {a['detail']}")
    print(f"Evidence: {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
