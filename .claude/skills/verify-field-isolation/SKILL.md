---
name: verify-field-isolation
description: Verify field_id is a true partition on the live knowledge store — no row references a row in a different field — and capture evidence. Asserts cross-field coherence (claim↔subject-entity/source, relationship↔endpoints/evidence, belief↔supporting/contradicting claims, investigation↔target/related/opening/resolution refs) plus orphan-field detection, writing a timestamped PASS/FAIL evidence report. Use after adding/onboarding a field, an entity reconcile/merge, a multi-field pipeline run, a migration touching field_id, or when asked to prove fields don't leak into each other.
---

# verify-field-isolation

Verify that **`field_id` is a partition, not a content axis** (Phase 17) on the
*live* Postgres knowledge store. Every knowledge row carries a `field_id`; the
core invariant is that a row must never reference a row in a *different* field.
This finds any cross-field reference already on disk and leaves an evidence
report behind.

This complements `tests/test_field_isolation.py` (which proves resolution and
memory never cross fields at the *application* level, on synthetic data). This
skill checks the *data that's actually there*.

## Invariants asserted

`check_field_isolation.py` runs these read-only assertions (PASS = zero
cross-field references):

- **claim_field_matches_subject_entity / claim_field_matches_source** — a claim shares its field with the entity it's about and the source it came from.
- **relationship_field_matches_endpoints** — a relationship shares its field with both endpoint entities.
- **relationship_evidence_claim_field_matches** — every evidence claim shares the relationship's field.
- **belief_supporting_claim_field_matches / belief_contradicting_claim_field_matches** — every cited claim shares the belief's field.
- **investigation_field_matches_target_entity / _related_entities / _opened_belief / _resolution_belief** — an investigation shares its field with every entity/belief it points at (these are not FK-enforced, so they're the highest-value checks).
- **all_field_ids_reference_a_real_field** — every `field_id` in use resolves to a row in `catalog.fields` (no orphan partitions).

> `belief_revisions` and `llm_usage` carry no `field_id` of their own — they
> inherit it through their head FK (belief / pipeline run), so there's nothing to
> cross-check on them directly.

## Steps

1. **Confirm the store is reachable.** The script reads the same DB env the app
   uses (`MESH_PG_READER_URL` → `MESH_PG_URL` → `LANGGRAPH_POSTGRES_URL`). For the
   docker stack make sure `mesh-postgres` is up (`make up` / `docker compose ps`).

2. **Run the checker.** Two equivalent paths:

   **(a) Python checker** (preferred — writes a full evidence dir):

   ```bash
   uv run python .claude/skills/verify-field-isolation/check_field_isolation.py
   ```

   It prints a per-assertion PASS/FAIL summary, writes evidence to
   `.evidence/verify-field-isolation/<UTC-timestamp>/` (`report.md` +
   `report.json`, including a per-field row-count table for context), and exits
   non-zero if anything failed.

   **(b) Pure-SQL fallback** (zero deps) — for a docker-only deployment:

   ```bash
   docker compose exec -T mesh-postgres \
     psql -U langgraph -d langgraph -f - \
     < .claude/skills/verify-field-isolation/assertions.sql \
     | tee ".evidence/verify-field-isolation/$(date -u +%Y%m%dT%H%M%SZ)-sql.txt"
   ```

   (`assertions.sql` is kept in sync with the Python checker; adjust the
   user/db/service name to your compose setup.)

3. **Read the evidence.** Open `report.md`. For any FAIL, the report includes up
   to 5 sample offending rows showing the two disagreeing `field_id` values —
   quote them.

4. **If something failed, diagnose — don't auto-fix.** A cross-field reference
   almost always traces to a write path that forgot to thread `field_id` (a new
   connector, a synthesis/relationship write, an investigation handler) or a
   merge that crossed fields (which must never happen). Surface the offending
   ids + the two fields involved; only mutate data if the user asks.

5. **Report** the verdict + evidence path, e.g.:
   `verify-field-isolation: FAIL — 2 beliefs cite claims from another field (evidence: .evidence/verify-field-isolation/<ts>/report.md)`.

## Notes

- Strictly read-only (uses the `mesh_reader` role). Safe to run anytime.
- A single-field store (just the seeded `ai-robotics` field) still exercises every
  check — they pass vacuously when there's nothing cross-field to find.
- Adding a check: append an `Assertion` (offending-rows SQL, PASS = 0 rows) to
  `ASSERTIONS` in `check_field_isolation.py` and mirror it in `assertions.sql`.
- Pair with `/verify-invariants` for the non-field data-integrity invariants.
