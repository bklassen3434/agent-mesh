---
name: verify-invariants
description: Verify the live knowledge store's data-integrity invariants and capture evidence. Asserts claim immutability/supersession, append-only belief revisions, no dangling array-provenance refs (post entity-merge), and claim_type↔predicate consistency — writing a timestamped PASS/FAIL evidence report. Use after a pipeline run, an entity reconcile/merge, a migration, or any change touching claims/beliefs/revisions/relationships, or when asked to verify data integrity / check the store is consistent.
---

# verify-invariants

Verify that the **core data-integrity invariants** hold on the *live* Postgres
knowledge store, and leave behind an evidence report. This is not pytest — it
runs against whatever data is actually in the store right now and records what it
observed.

## Invariants asserted

The bundled `check_invariants.py` runs these read-only assertions (PASS = zero
violations):

- **claim_supersession_pointer** — a `superseded` claim points at its successor; no claim supersedes itself (claims are immutable, superseded never deleted).
- **revision_count_matches_rows** — `beliefs.revision_count` equals the count of append-only `belief_revisions` rows.
- **belief_supporting_claims_exist / belief_contradicting_claims_exist / revision_trigger_claims_exist / relationship_evidence_claims_exist** — every id in these `text[]` provenance arrays references a real claim. These arrays are *not* FK-enforced, so an entity merge or claim delete can leave a dangling ref — this is the highest-value check after `reconcile-entities`.
- **no_self_relationships** — an entity merge must not collapse a relationship into a `from == to` self-loop.
- **held_belief_has_support** — a currently-held belief rests on ≥1 supporting claim.
- **claim_type_matches_predicate** — `claim_type` is the deterministic image of `predicate` (`PREDICATE_TO_CLAIM_TYPE`); unknown predicates park in the inert `speculative` bucket.

## Steps

1. **Confirm the store is reachable.** The script reads the same DB env the app
   uses (`MESH_PG_READER_URL` → `MESH_PG_URL` → `LANGGRAPH_POSTGRES_URL`). If the
   stack runs in docker, make sure `mesh-postgres` is up (`make up` / `docker
   compose ps`). For a local DB, ensure the env var is exported.

2. **Run the checker.** Two equivalent paths — pick the one that matches how the
   store is reachable:

   **(a) Python checker** (preferred — writes a full evidence dir). Needs the DB
   env reachable from where you run it (same precondition as `mesh.cli`):

   ```bash
   uv run python .claude/skills/verify-invariants/check_invariants.py
   ```

   It prints a per-assertion PASS/FAIL summary, writes evidence to
   `.evidence/verify-invariants/<UTC-timestamp>/` (`report.md` + `report.json`),
   and exits non-zero if anything failed.

   **(b) Pure-SQL fallback** (zero deps) — for the docker-only deployment where
   Postgres isn't reachable from the host. Runs the *same* invariants via psql and
   prints an `invariant | violations | result` table:

   ```bash
   docker compose exec -T mesh-postgres \
     psql -U langgraph -d langgraph -f - \
     < .claude/skills/verify-invariants/assertions.sql \
     | tee ".evidence/verify-invariants/$(date -u +%Y%m%dT%H%M%SZ)-sql.txt"
   ```

   (`assertions.sql` is kept in sync with the Python checker; adjust the
   user/db/service name to your compose setup.)

3. **Read the evidence, don't just trust the exit code.** Open the printed
   `report.md`. For any FAIL, the report includes up to 5 sample offending rows
   (ids) — quote them in your summary.

4. **If something failed, diagnose — don't auto-fix.** Dangling provenance refs
   usually trace to a recent `reconcile-entities --apply` or a manual delete;
   `revision_count` drift points at a synthesis/revision write path. Surface the
   offending ids and the likely cause; only mutate data if the user asks.

5. **Report** the verdict + evidence path in your final message, e.g.:
   `verify-invariants: FAIL — 3 dangling supporting_claim_ids on belief b_… (evidence: .evidence/verify-invariants/<ts>/report.md)`.

## Notes

- Strictly read-only (uses the `mesh_reader` role). Safe to run anytime.
- Adding an invariant: append an `Assertion` (offending-rows SQL, PASS = 0 rows)
  to `ASSERTIONS` in `check_invariants.py`. Keep each one a single query that
  returns *only* violations.
