---
name: verify-entity-resolution
description: Verify an entity-reconcile pass merged duplicates without corrupting the store, and capture evidence. Snapshots before, runs mesh.cli reconcile-entities (dry-run then --apply), snapshots after, asserts the merge only shrank entities/relationships while leaving the claim set byte-identical (a merge re-points claims.subject_entity_id, never adds/deletes a claim), and checks the live store for post-merge corruption (self-loops, dangling investigation entity refs). Use after a reconcile, before/after a merge, or when asked to prove entity resolution didn't damage data.
---

# verify-entity-resolution

Verify that **semantic entity resolution** (Phase 13) did its job — absorb
duplicate entities, re-point their references, aggregate colliding edges — and
did it *safely*: claims are immutable, so a merge may re-point
`claims.subject_entity_id` but must never add or delete a claim, and it must not
strand the non-FK references (`investigations.target_entity_id` /
`related_entity_ids`) that a deleted duplicate leaves behind.

`merge_entities` re-points claims/relationships/investigations from the duplicate
to the canonical, aggregates colliding edges, deletes self-loops, folds aliases,
then deletes the duplicate row. This skill proves the *result* is sound.

Requires the Postgres store reachable (`make up`). The dry-run is read-only; only
`--apply` writes.

> Run against a dev store. A reconcile with `--apply` mutates entities and
> re-points references — it's the one write this skill makes.

## Steps

1. **Pick an evidence dir:**

   ```bash
   TS=$(date -u +%Y%m%dT%H%M%SZ); EV=.evidence/verify-entity-resolution/$TS; mkdir -p "$EV"
   ```

2. **Snapshot BEFORE:**

   ```bash
   uv run python .claude/skills/verify-entity-resolution/snapshot.py "$EV/before.json"
   ```

3. **Dry-run first** (read-only — see what it *would* merge, capture the report):

   ```bash
   uv run mesh.cli reconcile-entities --field ai-robotics --report "$EV/reconcile-dryrun.md" 2>&1 | tee "$EV/dryrun.log"
   ```

   The printed line is `before=… after=… merges=… auto=… adjudicated=… embedded=…`.
   If `merges=0`, there's nothing to verify — note the vacuous PASS and stop.

4. **Apply** (writes — performs the merges):

   ```bash
   uv run mesh.cli reconcile-entities --field ai-robotics --apply --report "$EV/reconcile-apply.md" 2>&1 | tee "$EV/apply.log"
   ```

5. **Snapshot AFTER:**

   ```bash
   uv run python .claude/skills/verify-entity-resolution/snapshot.py "$EV/after.json"
   ```

6. **Assert merge soundness:**

   ```bash
   uv run python .claude/skills/verify-entity-resolution/check_resolution.py "$EV/before.json" "$EV/after.json"
   ```

   It writes its own evidence dir and asserts (PASS only if all hold):

   **Delta (before vs after):**
   - **entities_non_increasing / relationships_non_increasing** — merges only absorb; counts never grow.
   - **claims_count_unchanged / claim_id_set_unchanged** — the claim set is byte-identical (the immutability-under-merge guarantee; re-pointing a FK is not a content change).
   - **investigations_count_unchanged** — references are re-pointed, not investigations added/removed.
   - **null_embeddings_non_increasing** — reconcile backfills `name_embedding`, never un-embeds.

   **Structural (live store, post-apply):**
   - **no_self_relationships** — the merge deleted any self-loop it created.
   - **no_dangling_investigation_target_entity / _related_entities** — no investigation references a deleted duplicate (these are not FK-enforced — the highest-value post-merge check).

7. **Re-assert invariants** on the post-merge store (catches dangling *claim-id*
   array refs on beliefs/relationships/revisions — the complement of the entity
   refs checked above):

   ```bash
   uv run python .claude/skills/verify-invariants/check_invariants.py
   uv run python .claude/skills/verify-field-isolation/check_field_isolation.py
   ```

   `/verify-field-isolation` confirms the merge never crossed a field boundary.

8. **Report** the combined verdict + `$EV` path. Quote the before→after entity
   count, the merge count from the apply log, and any structural violation. A
   `claim_id_set_unchanged` FAIL is serious — it means a merge deleted or created
   a claim, violating immutability.

## Notes

- `snapshot.py` and the dry-run are read-only; only `--apply` writes.
- Tune blocking breadth with `--k` (default 10); the auto/adjudicate bands are
  `MESH_ENTITY_MERGE_HIGH` / `_LOW`.
- Delta math assumes no concurrent writer between the two snapshots.
- Pair with `/verify-invariants`, `/verify-field-isolation` (step 7).
