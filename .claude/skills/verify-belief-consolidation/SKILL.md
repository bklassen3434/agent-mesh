---
name: verify-belief-consolidation
description: Verify a belief-consolidation pass stayed strictly append-only and capture evidence. Snapshots before, runs mesh.cli consolidate-beliefs (dry-run then --apply), snapshots after, asserts the headline Phase 19 guarantee — no belief or revision row ever deleted, claims untouched, merged-away beliefs marked is_currently_held=false with a revision, confidence stays in range — then re-runs the invariant checks. Use after consolidating beliefs, before/after the decay/archival pass, or when asked to prove consolidation absorbed duplicates without erasing anything.
---

# verify-belief-consolidation

Verify that **belief consolidation** (Phase 19) — the world-model analog of entity
resolution, but **strictly append-only** — behaves. Unlike an entity merge (which
deletes the duplicate row), a belief merge *absorbs*: the merged-away belief is
marked `is_currently_held=false` but keeps its row and every revision, and each
change appends a `belief_revision` attributed to `belief_consolidator`. Migration
011 deliberately grants **no DELETE**, so "append-only" is enforced at the DB
level — this skill proves the data on disk honors it.

The pass has two parts: semantic de-duplication of held beliefs (block → match →
merge) and an LLM-free decay/archival pass that ages stale beliefs.

Requires the Postgres store reachable (`make up`). Middle-band merge adjudication
uses an LLM (`ANTHROPIC_API_KEY`); without one, only high-band auto-merges run.
The dry-run is read-only; only `--apply` writes.

> Run against a dev store. Needs held beliefs to consolidate — run
> `/verify-pipeline` first if the store is empty (else a vacuous PASS).

## Steps

1. **Pick an evidence dir:**

   ```bash
   TS=$(date -u +%Y%m%dT%H%M%SZ); EV=.evidence/verify-belief-consolidation/$TS; mkdir -p "$EV"
   ```

2. **Snapshot BEFORE:**

   ```bash
   uv run python .claude/skills/verify-belief-consolidation/snapshot.py "$EV/before.json"
   ```

3. **Dry-run first** (read-only — what it *would* merge/decay/archive):

   ```bash
   uv run mesh.cli consolidate-beliefs --field ai-robotics --report-path "$EV/consolidate-dryrun.md" 2>&1 | tee "$EV/dryrun.log"
   ```

   The printed line is `held before=… after=… merges=… auto=… adjudicated=…
   decayed=… archived=… embedded=…`. If `merges=0 decayed=0 archived=0`, there's
   nothing to verify — note the vacuous PASS and stop.

4. **Apply** (writes — performs merges + decay/archival):

   ```bash
   uv run mesh.cli consolidate-beliefs --field ai-robotics --apply --report-path "$EV/consolidate-apply.md" 2>&1 | tee "$EV/apply.log"
   ```

   (Add `--no-decay` to verify the merge pass in isolation.)

5. **Snapshot AFTER:**

   ```bash
   uv run python .claude/skills/verify-belief-consolidation/snapshot.py "$EV/after.json"
   ```

6. **Assert append-only + consistency:**

   ```bash
   uv run python .claude/skills/verify-belief-consolidation/check_deltas.py "$EV/before.json" "$EV/after.json"
   ```

   It writes its own evidence dir and asserts (PASS only if all hold):

   **Append-only delta (the headline):**
   - **beliefs_count_unchanged** — no belief deleted (and none created).
   - **belief_revisions_non_decreasing / consolidator_revisions_non_decreasing** — revisions only accumulate; every change appends one attributed to `belief_consolidator`.
   - **held_beliefs_non_increasing** — merge/archive only un-holds; nothing re-holds.
   - **claims_count_unchanged / claim_id_set_unchanged** — consolidation never touches claims.
   - **unheld_beliefs_have_revisions** — every belief that dropped out of "held" did so with a revision (no silent un-hold).

   **Live structural:**
   - **merged_beliefs_not_held** — every belief with a "merged into …" revision is not-held.
   - **confidence_in_unit_range** — decay floored confidence stays in [0, 1].
   - **investigation_opened/resolution_belief_refs_resolve** — re-pointed belief refs still resolve.

   > Delta math assumes no concurrent writer between the two snapshots.

7. **Re-assert invariants** (the most important being `revision_count_matches_rows`
   — consolidation bumps `revision_count` on every append):

   ```bash
   uv run python .claude/skills/verify-invariants/check_invariants.py
   ```

   A consolidation that left `revision_count` out of sync with the revision rows,
   or stranded a supporting-claim ref, is a FAIL even if it "succeeded".

8. **Report** the combined verdict + `$EV` path. Quote the held-before→after
   count, the merges/decayed/archived numbers from the apply log, and the
   append-only verdict. A `beliefs_count_unchanged` or `claim_id_set_unchanged`
   FAIL is severe — it means consolidation deleted a row or touched a claim,
   breaking the core invariant.

## Notes

- `snapshot.py` and the dry-run are read-only; only `--apply` writes.
- Decay/archival knobs: `MESH_BELIEF_DECAY_HALFLIFE_DAYS`, `MESH_BELIEF_DECAY_FLOOR`,
  `MESH_BELIEF_ARCHIVE_AFTER_DAYS`; merge bands: `MESH_BELIEF_MERGE_HIGH` / `_LOW`.
- `mesh.cli beliefs duplicates --field ai-robotics` gives a read-only candidate-pair
  view to sanity-check what *should* merge before applying.
- Pair with `/verify-invariants` (step 7) and `/verify-field-isolation`.
