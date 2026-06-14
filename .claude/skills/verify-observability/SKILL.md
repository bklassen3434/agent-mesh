---
name: verify-observability
description: Verify the model-routing (Phase 20) and autonomous-discovery (Phase 22) observability surfaces on the live store, and capture evidence. Hard-asserts LLM-ledger integrity (non-negative tokens/cost, every usage row references a real run), valid investigation origins, and discovery provenance (every discovery investigation has a trigger_rationale); and reports the per-model tier split of LLM spend (cheap vs strong) plus discovery activity. Writes a timestamped PASS/FAIL evidence report. Use after enabling routing, a discovery sweep, or when asked to check routing/discovery is behaving and what it cost.
---

# verify-observability

Verify the two "what is the system doing and what did it cost" surfaces —
**tiered model routing** (Phase 20, the `llm_usage` ledger) and **autonomous
discovery** (Phase 22, `origin='discovery'` investigations) — against the *live*
store. Two jobs in one read-only pass:

- **VERIFY** — hard assertions that must hold; PASS = zero violations.
- **REPORT** — informational context with no single right answer (the per-model
  tier split of spend, discovery activity) captured alongside the verdict.

The report context never flips the verdict — only a hard assertion can.

## Hard assertions (PASS = zero violations)

- **llm_usage_tokens_non_negative / llm_usage_cost_non_negative** — the LLM cost ledger never records negative tokens or cost.
- **llm_usage_run_id_references_run** — every `llm_usage` row references a real `pipeline_runs` row (the ledger joins to runs for per-run cost + field scoping; a dangling `run_id` breaks `routing-stats`).
- **investigation_origin_valid** — `investigations.origin` is one of `curator|skeptic|discovery|manual`.
- **discovery_investigation_has_rationale** — every `origin='discovery'` investigation carries a `trigger_rationale` (autonomous self-direction must be explainable/auditable).

## Reported context (not part of the verdict)

- **Routing tier split** — `llm_usage` grouped by `model`, each mapped to a tier (`cheap` = `MESH_ROUTE_CHEAP_MODEL`/`*haiku*`, `strong` = `MESH_ROUTE_STRONG_MODEL`/`*sonnet*`/`*opus*`, else `other`/`unrecorded`), with calls, tokens, and cost. This is the same data `mesh.cli routing-stats` reports — eyeball that escalations to the strong tier are the exception, not the rule.
- **Null-model ledger rows** — count of `llm_usage` rows with no recorded `model` (Phase 20 records the *realized* model; a rising count is worth a look but isn't a hard failure).
- **Discovery activity** — investigations grouped by `origin`, and the most recent `run_type='discovery'` runs.

## Steps

1. **Confirm the store is reachable** (`MESH_PG_READER_URL` → `MESH_PG_URL` →
   `LANGGRAPH_POSTGRES_URL`; for docker, `mesh-postgres` up).

2. **Run the checker:**

   ```bash
   uv run python .claude/skills/verify-observability/check_observability.py
   ```

   It prints the hard-assertion verdict, the routing tier split, and the
   investigations-by-origin breakdown; writes evidence to
   `.evidence/verify-observability/<UTC-timestamp>/` (`report.md` +
   `report.json`); and exits non-zero only if a hard assertion fails.

   To classify tiers against non-default model ids, export `MESH_ROUTE_CHEAP_MODEL`
   / `MESH_ROUTE_STRONG_MODEL` to match your config before running.

3. **Cross-check the CLIs (optional but recommended).** The report mirrors what
   these print live — run them to corroborate or to scope by field/time:

   ```bash
   uv run mesh.cli routing-stats --field ai-robotics --since 7d
   uv run mesh.cli discover --field ai-robotics            # dry-run: gaps + hypotheses it would open
   ```

4. **Read the evidence.** Open `report.md`. For any hard FAIL, the report
   includes up to 5 sample offending rows — quote them. Skim the tier-split and
   origin tables for anything surprising (e.g. all spend on the strong tier, or a
   discovery investigation with no rationale).

5. **If something failed, diagnose — don't auto-fix.** A dangling `run_id` points
   at a ledger write that outran its run row; a discovery investigation missing a
   rationale points at a discovery write path that skipped provenance. Surface
   the ids; only mutate data if the user asks.

6. **Report** the verdict + evidence path, plus the headline tier split, e.g.:
   `verify-observability: PASS — 247 cheap / 12 strong calls, $0.41 total; 5 discovery investigations (evidence: .evidence/verify-observability/<ts>/report.md)`.

## Notes

- Strictly read-only (uses the `mesh_reader` role). Safe to run anytime.
- Routing ships off by default; with it off, every call lands on the cheap tier
  and the split is trivially one-tier — that's expected, not a failure.
- An empty ledger / no discovery investigations passes vacuously (the report
  tables just show "none").
- Bounds like `MESH_DISCOVER_MAX_NEW` are per-run caps; investigations carry no
  run id, so this skill reports discovery *volume* rather than asserting a
  per-run cap. Use `/verify-pipeline`-style before/after counting around a single
  `mesh-discover` run if you need to prove a specific run stayed under its cap.
