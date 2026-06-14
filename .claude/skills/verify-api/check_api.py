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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

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
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
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
        "graph_data": "/api/v1/graph/data",
        "agents": "/api/v1/agents",
        "agents_graph": "/api/v1/agents/graph",
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
        captures["belief_detail"] = {
            "path": f"/api/v1/beliefs/{bid}",
            "status": status,
            "body": detail,
        }
        (out / "belief_detail.json").write_text(
            json.dumps(captures["belief_detail"], indent=2, default=str)
        )
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

    # 6. graph/data (pre-aggregated, Phase 9): edges reference real nodes, ≤200 nodes
    gd = captures["graph_data"]
    if gd["status"] == 200 and isinstance(gd["body"], dict):
        gd_nodes = {n.get("id") for n in gd["body"].get("nodes", [])}
        gd_edges = gd["body"].get("edges", [])
        gd_dangling = [
            e
            for e in gd_edges
            if e.get("source") not in gd_nodes or e.get("target") not in gd_nodes
        ]
        record(
            "graph_data_edges_reference_real_nodes",
            not gd_dangling,
            f"{len(gd_edges)} edges, {len(gd_nodes)} nodes, {len(gd_dangling)} dangling",
        )
        record(
            "graph_data_node_cap",
            len(gd_nodes) <= 200,
            f"{len(gd_nodes)} nodes (cap 200)",
        )
    else:
        record("graph_data_edges_reference_real_nodes", False, f"graph/data -> {gd['status']}")

    # 7. agents roster (Phase 23): sample an agent, its invocations resolve to it,
    #    and a sampled invocation's detail resolves its applied heuristics.
    roster = _items(captures["agents"]["body"])
    if roster:
        agent_name = roster[0].get("agent")
        inv_path = f"/api/v1/agents/{quote(str(agent_name), safe='')}/invocations?limit=200"
        status, invs = get(inv_path)
        captures["agent_invocations"] = {"path": inv_path, "status": status, "body": invs}
        (out / "agent_invocations.json").write_text(
            json.dumps(captures["agent_invocations"], indent=2, default=str)
        )
        inv_items = invs if isinstance(invs, list) else []
        if status == 200:
            mismatched = [i for i in inv_items if i.get("agent") != agent_name]
            record(
                "agent_invocations_match_agent",
                not mismatched,
                f"agent {agent_name}: {len(inv_items)} invocations, {len(mismatched)} mismatched",
            )
        else:
            record("agent_invocations_match_agent", False, f"{inv_path} -> {status}")

        # Sample an invocation that injected heuristics; verify its detail resolves
        # only ids the invocation actually applied.
        sample = next((i for i in inv_items if i.get("applied_heuristic_ids")), None)
        if sample:
            iid = sample.get("id")
            d_path = f"/api/v1/agents/invocations/{quote(str(iid), safe='')}"
            d_status, detail = get(d_path)
            captures["invocation_detail"] = {"path": d_path, "status": d_status, "body": detail}
            (out / "invocation_detail.json").write_text(
                json.dumps(captures["invocation_detail"], indent=2, default=str)
            )
            if d_status == 200 and isinstance(detail, dict):
                applied_ids = set(sample.get("applied_heuristic_ids", []) or [])
                resolved = {h.get("id") for h in detail.get("applied_heuristics", []) or []}
                stray = resolved - applied_ids
                record(
                    "invocation_detail_heuristics_consistent",
                    not stray,
                    f"invocation {iid}: {len(applied_ids)} applied, {len(resolved)} resolved",
                )
            else:
                record("invocation_detail_heuristics_consistent", False, f"detail -> {d_status}")
        else:
            record(
                "invocation_detail_heuristics_consistent",
                True,
                "no invocation with applied heuristics to sample (vacuously ok)",
            )
    else:
        record("agent_invocations_match_agent", True, "no agents in roster (vacuously ok)")
        record("invocation_detail_heuristics_consistent", True, "no agents (vacuously ok)")

    # 8. agent interaction graph is a coordinator star: ≤1 coordinator node, every
    #    edge sourced at it, no dangling endpoints.
    ag = captures["agents_graph"]
    if ag["status"] == 200 and isinstance(ag["body"], dict):
        ag_nodes = ag["body"].get("nodes", [])
        ag_edges = ag["body"].get("edges", [])
        ag_node_ids = {n.get("id") for n in ag_nodes}
        coord_ids = {n.get("id") for n in ag_nodes if n.get("role") == "coordinator"}
        non_star = [e for e in ag_edges if e.get("source") not in coord_ids]
        ag_dangling = [
            e
            for e in ag_edges
            if e.get("source") not in ag_node_ids or e.get("target") not in ag_node_ids
        ]
        ok = len(coord_ids) <= 1 and not non_star and not ag_dangling
        record(
            "agent_graph_star_topology",
            ok,
            f"{len(coord_ids)} coordinator node(s), {len(non_star)} non-star "
            f"edge(s), {len(ag_dangling)} dangling",
        )
    else:
        record("agent_graph_star_topology", False, f"agents/graph -> {ag['status']}")

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
