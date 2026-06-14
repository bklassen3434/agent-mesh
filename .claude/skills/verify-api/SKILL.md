---
name: verify-api
description: Verify the running read API (apps/api, :8000) serves internally-consistent state and capture evidence. Hits /healthz, /api/v1/stats, /beliefs, /claims, /graph, /graph/data and /agents (+ sampled belief detail, agent invocations, agent graph), asserts cross-response consistency (graph edges reference real nodes, pagination totals sane, belief detail resolves its cited claims, agent invocations resolve to their agent, the agent graph is a coordinator star), and writes a timestamped PASS/FAIL evidence report. Use after changing the API, before pushing API/wiki work, or when asked to verify the API works / is healthy / serves consistent data.
---

# verify-api

Verify the read API serves **internally-consistent** state and leave behind the
captured responses as evidence. This goes beyond `GET /healthz` — it cross-checks
responses against each other (e.g. every graph edge points at a node that's
actually in the payload).

## Assertions

The bundled `check_api.py` captures live responses, writes each to the evidence
dir, then asserts:

- **api_reachable / healthz_status_ok** — `/healthz` returns 200 with `status: ok`.
- **stats_available / stats_counts_non_negative** — `/api/v1/stats` returns counts, none negative.
- **graph_edges_reference_real_nodes** — every `/api/v1/graph` edge's `source`/`target` is present in that payload's `nodes` (the highest-value cross-consistency check).
- **beliefs_page_total_sane / claims_page_total_sane** — paginated `total` ≥ the number of returned items.
- **belief_detail_claims_consistent** — a sampled belief detail's resolved `supporting_claims` are all ids the belief actually cites.
- **graph_data_edges_reference_real_nodes / graph_data_node_cap** — the pre-aggregated `/api/v1/graph/data` (Phase 9) has no dangling edges and respects its top-200-node cap.
- **agent_invocations_match_agent** — every invocation returned for a sampled roster agent (`/api/v1/agents` → `/api/v1/agents/{agent}/invocations`) is attributed to that agent (Phase 23 observability).
- **invocation_detail_heuristics_consistent** — a sampled invocation's detail resolves only heuristic ids the invocation actually applied.
- **agent_graph_star_topology** — `/api/v1/agents/graph` is a coordinator star: ≤1 coordinator node, every edge sourced at it, no dangling endpoints.

## Steps

1. **Make sure the API is running** on the target base URL (default
   `http://localhost:8000`). Either `uv run mesh-api` locally or `make up` /
   `docker compose up` for the stack. Override with `API_BASE` if it's elsewhere.

2. **Run the checker** from the repo root:

   ```bash
   uv run python .claude/skills/verify-api/check_api.py
   # or against a non-default origin:
   API_BASE=http://localhost:8000 uv run python .claude/skills/verify-api/check_api.py
   ```

   It prints a per-assertion summary, writes evidence to
   `.evidence/verify-api/<UTC-timestamp>/` (`report.md`, `report.json`, and one
   `*.json` per captured endpoint), and exits non-zero on any failure or if the
   API is unreachable.

3. **Inspect the captured responses** in the evidence dir for any FAIL — the raw
   JSON is on disk, so you can show exactly which edge dangled or which count was
   off.

4. **Report** the verdict + evidence path. If `api_reachable` failed, say the API
   is down and how to start it; don't report the other assertions as meaningful.

## Notes

- Read-only and stdlib-only (no extra deps) — uses `urllib`.
- The API is served by the read-only `mesh_reader` role, so this can't mutate the
  store regardless.
- An empty store is fine: assertions are written to pass vacuously (e.g. no
  beliefs to sample) rather than error.
