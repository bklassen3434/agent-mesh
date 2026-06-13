---
name: verify-pipeline
description: Verify a bounded pipeline cycle actually does what it claims and capture evidence. Snapshots store state before, runs a small mesh-pipeline run, snapshots after, asserts the deltas are internally consistent (sources→claims→entities→beliefs, recorded errors), then re-runs the invariant checks on the freshly-written data. Use to verify the pipeline end-to-end, confirm a coordinator/extractor/synthesis change works on real data, or when asked to prove a pipeline run produced sane output (not just that it exited 0).
---

# verify-pipeline

Verify that a **pipeline cycle** produces internally-consistent output — not just
that `mesh-pipeline` exited cleanly. It captures before/after state, checks the
deltas line up with what the run *reported*, and then asserts the core data
invariants on the newly-written rows. Evidence is recorded at each step.

Requires a working LLM provider (`ANTHROPIC_API_KEY`, or `MESH_LLM_PROVIDER=ollama`
with Ollama up) and the Postgres store reachable (`make up` for the stack).

> Use a **bounded** run (`--max-papers` small) so the cycle is quick and the
> deltas are easy to reason about. This skill *does* write to the store (that's
> the point); run it against a dev store, never production data you care about.

## Steps

1. **Pick an evidence dir** for this run:

   ```bash
   TS=$(date -u +%Y%m%dT%H%M%SZ); EV=.evidence/verify-pipeline/$TS; mkdir -p "$EV"
   ```

2. **Snapshot BEFORE:**

   ```bash
   uv run python .claude/skills/verify-pipeline/snapshot.py "$EV/before.json"
   ```

3. **Run a bounded pipeline** and tee its stdout (the reported deltas matter):

   ```bash
   uv run mesh-pipeline --max-papers 5 --since 7d 2>&1 | tee "$EV/run.log"
   ```

   Note the printed `Pipeline run <run_id>` block — claims/entities/beliefs
   inserted, avg latency, and the `Errors:` count + per-paper error lines.

4. **Snapshot AFTER:**

   ```bash
   uv run python .claude/skills/verify-pipeline/snapshot.py "$EV/after.json"
   ```

5. **Assert delta consistency** (compare `before.json`, `after.json`, and the
   latest run row inside `after.json`'s `latest_pipeline_run`). The verdict is
   PASS only if all hold:

   - The latest run's `id` in `after.json` differs from `before.json` (a new run was recorded).
   - `after.counts.claims - before.counts.claims == run.claims_inserted` (store delta matches the reported delta). Same shape for `sources_inserted`, `entities_created`, `beliefs_created`.
   - All counts are monotonic non-decreasing except where supersession/revision is expected.
   - If `claims_inserted > 0` then `sources_inserted > 0` (claims must come from a source).
   - The run's `errors` list is empty **or** every entry has a `paper_id` + `error_type` + message (a partial failure that was recorded, not a silent drop). Surface the count either way — a non-zero error count is a soft FAIL worth calling out, not necessarily a hard one.

   Write your findings to `$EV/report.md` as a PASS/FAIL table with the actual
   numbers (reported delta vs. observed store delta, side by side).

6. **Re-assert invariants on the new data** — the most important step, since this
   run just wrote claims/entities/beliefs:

   ```bash
   uv run python .claude/skills/verify-invariants/check_invariants.py
   ```

   Fold its verdict into your report (link its evidence dir). A green pipeline run
   that violates an invariant is a FAIL.

7. **Report** the combined verdict + `$EV` path. Quote the reported-vs-observed
   delta table and the invariant verdict. If the deltas disagree, that points at a
   write that bypassed the run accounting (or a concurrent writer) — call it out.

## Notes

- `snapshot.py` is read-only; only `mesh-pipeline` writes.
- For an even lighter check that touches no LLM, snapshot before/after a
  `POST /api/v1/pipelines/pipeline/trigger` instead — but that needs the scheduler
  up and still runs a real cycle.
- Pair with `/verify-invariants` (step 6) and `/verify-api` for full coverage.
