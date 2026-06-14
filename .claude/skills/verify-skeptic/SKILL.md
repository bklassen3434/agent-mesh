---
name: verify-skeptic
description: Verify a skeptic sweep actually challenged beliefs and capture evidence. Snapshots store state before, runs mesh-skeptic, snapshots after, asserts the deltas match what the run reported (critique counter-claims, skeptic-attributed belief revisions, synthetic agent_reasoning sources), then re-runs the invariant checks on the freshly-written data. Use to verify the skeptic end-to-end, confirm a skeptic/curator change works on real data, or when asked to prove a sweep produced sane output (not just that it exited 0).
---

# verify-skeptic

Verify that a **skeptic sweep** does what it claims — challenge held beliefs,
inserting critique counter-claims and revising belief confidence — not just that
`mesh-skeptic` exited cleanly. It captures before/after state, checks the
deltas line up with what the run *reported* in `pipeline_runs`, then asserts the
core data invariants on the newly-written rows.

When the skeptic applies an assessment it writes, per applied assessment: one
**critique counter-claim** (`claim_type='critique'`, `extracted_by_agent='skeptic'`),
one synthetic **agent_reasoning source** (`author='skeptic'`), and one
**belief_revision** (`revised_by_agent='skeptic'`) — recorded on the run as
`claims_inserted` / `sources_inserted` / `beliefs_revised`.

Requires a working LLM provider (`ANTHROPIC_API_KEY`, or `MESH_LLM_PROVIDER=ollama`
with Ollama up) and the Postgres store reachable (`make up`).

> This skill **writes** to the store (that's the point) — run it against a dev
> store. It needs held beliefs to challenge: run `/verify-pipeline` first if the
> store is empty, or the sweep will have nothing to do (a vacuous PASS).

## Steps

1. **Pick an evidence dir** for the run snapshots:

   ```bash
   TS=$(date -u +%Y%m%dT%H%M%SZ); EV=.evidence/verify-skeptic/$TS; mkdir -p "$EV"
   ```

2. **Snapshot BEFORE:**

   ```bash
   uv run python .claude/skills/verify-skeptic/snapshot.py "$EV/before.json"
   ```

3. **Run the sweep** and tee its stdout (the reported deltas matter):

   ```bash
   uv run mesh-skeptic --field ai-robotics 2>&1 | tee "$EV/run.log"
   ```

   Note the printed run block — counter-claims, beliefs revised, errors.

4. **Snapshot AFTER:**

   ```bash
   uv run python .claude/skills/verify-skeptic/snapshot.py "$EV/after.json"
   ```

5. **Assert delta consistency:**

   ```bash
   uv run python .claude/skills/verify-skeptic/check_deltas.py "$EV/before.json" "$EV/after.json"
   ```

   It writes its own evidence dir and asserts (PASS only if all hold):

   - **new_skeptic_run_recorded** — a fresh `run_type='skeptic'` row exists.
   - **critique_claims_delta_matches_run** — store Δ critique claims == reported `claims_inserted`.
   - **skeptic_revisions_delta_matches_run** — store Δ skeptic revisions == reported `beliefs_revised`.
   - **skeptic_sources_delta_matches_run** — store Δ agent_reasoning sources == reported `sources_inserted`.
   - **claims/belief_revisions/beliefs monotonic** — nothing was deleted.
   - **run_errors_wellformed** — every recorded error has `paper_id`+`error_type`+`error_message` (a recorded partial failure, not a silent drop). A non-zero error count is worth calling out.

   > Delta math assumes no concurrent writer during the sweep (counts are
   > store-wide; `belief_revisions` carry no `field_id`). Run sweeps one at a time.

6. **Re-assert invariants on the new data** — the sweep just wrote claims and
   revisions, so verify they didn't violate anything:

   ```bash
   uv run python .claude/skills/verify-invariants/check_invariants.py
   ```

   A green sweep that violates an invariant (e.g. a `revision_count` that no
   longer matches its rows, or a contradicting-claim ref that dangles) is a FAIL.
   Fold its verdict into your summary.

7. **Report** the combined verdict + `$EV` path. Quote the reported-vs-observed
   delta table and the invariant verdict. A sweep that revised zero beliefs (no
   eligible held beliefs / all in cooldown) is a vacuous PASS — say so.

## Notes

- `snapshot.py` is read-only; only `mesh-skeptic` writes.
- Scope the sweep with `--field` (default `ai-robotics`); behavior knobs live in
  env (`MESH_SKEPTIC_APPLY_THRESHOLD`, `MESH_CURATOR_PICK_COUNT`,
  `MESH_CURATOR_COOLDOWN_DAYS`).
- Pair with `/verify-invariants` (step 6) and `/verify-field-isolation` for full
  coverage of what a sweep touched.
