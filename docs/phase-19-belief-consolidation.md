# Phase 19 — Belief Consolidation: Semantic Dedup, Merge & Decay

## Context

Agent Mesh is an A2A-based multi-agent research-tracking system on Postgres,
now field-agnostic (Phase 17) with a self-serve connector layer (Phase 18).
Today three maintenance mechanisms exist, on three different layers:

- **Entity resolution (Phase 13)** cleans the graph's *nodes* — semantic
  dedup/merge of `entities` (block → match → merge), inline in the coordinator
  before any entity is created, plus a one-time `reconcile-entities` backfill.
- **The memory-consolidation job (Phase 16c)** cleans the *agents' know-how* — a
  scheduled LangGraph job (`mesh-consolidate-memory`, `apps/pipeline/consolidation.py`)
  that distills episodic history into procedural `agent_heuristics` rows. **This
  is not belief consolidation** — do not confuse the two; this phase adds a
  *separate* job (`mesh-consolidate-beliefs`).
- **Belief synthesis (Phase 14)** writes the *facts* — the coordinator's
  `synthesize` node turns claims into `beliefs` (`score`/`capability`/relational),
  with confidence derived from `belief_signals` via
  `mesh_agents.confidence.compute_confidence`.

The gap: **nothing cleans the beliefs themselves.** `score` and
`capability:<entity_id>` beliefs converge deterministically, but
semantically-equivalent beliefs synthesized under different `topic` strings (or
anchored on two entities that were only later merged) accumulate as duplicates;
and beliefs never decay or retire — a SOTA belief from eight months ago stays
`is_currently_held = true` at full confidence. `find_stale_beliefs` *flags*
staleness for the Curator to open Investigations, but nothing consolidates or
ages the corpus.

This phase is the **world-model analog of entity resolution**, packaged in the
**job shape of the Phase 16c consolidation graph**: a scheduled batch sweep that
semantically de-duplicates beliefs (block → match → merge), then ages stale ones
— all coordinator-owned, all append-only, all provenance-grounded, all
field-scoped (Phase 17). Built in one pass, five logical blocks (19a–19e), **no
measurement gate between blocks** — tags are commit checkpoints only.

Read before writing any code — do not guess table, column, function, graph, or
route details:

- Entity resolution end-to-end: `mesh_db.entities`
  (`_vector_literal`, `set_entity_embedding`, `find_candidate_duplicates`,
  `choose_canonical`, `merge_entities`), `mesh_agents.entity_resolution`
  (`ResolutionConfig`, `band`, `resolve_entity_semantic`),
  `mesh_agents.reconcile` (the backfill + report), and migration
  `006_entity_resolution.sql`
- `mesh_llm.embeddings` — the `Embedder` protocol, `entity_embed_text`,
  `make_embedder`, `FastEmbedEmbedder` (BAAI/bge-small-en-v1.5, 384-dim, `EMBED_DIM`)
- The belief write path: `mesh_db.beliefs`
  (`create_belief`, `update_belief`, `get_belief_signals`, `find_stale_beliefs`,
  `list_beliefs`), `mesh_db.revisions` (`create_revision`), the
  `Belief`/`BeliefRevision` models, and the coordinator `synthesize` node
  (`apps/pipeline/coordinator.py`: `_run_sota`, `_run_capability`, `_belief_confidence`)
- `mesh_agents.confidence.compute_confidence` and the `belief_signals` view
  (migration `004_derived_signal_views.sql`)
- The Phase 16c job: `apps/pipeline/consolidation.py` (cloned from
  `skeptic_sweep.py`), its `mesh-consolidate-memory` entry point, the batch/sync path,
  the `MESH_CONSOLIDATION_BATCH` flags, the finalize-idempotency guard
  (`pipeline_run_exists`), `open_checkpointer`, traceparent, and Langfuse cost
  attribution
- The scheduler wiring: `JOB_COMMANDS` in
  `apps/scheduler/src/mesh_scheduler/scheduler.py`, `DEFAULT_INTERVALS` +
  the `schedules` table (`mesh_a2a.schedules`), and the `consolidate` target in
  the `Makefile`
- The Anthropic batch surface: `AnthropicClient.submit_batch` / `batch_status` /
  `collect_batch`, `BatchRequestItem` / `BatchItemResult` (`mesh_llm.batch`)
- The coordinator-writer / api-readonly Postgres roles and the grants in
  `005_grants.sql` / `006_entity_resolution.sql`

---

## Goal

A scheduled, offline LangGraph job (`mesh-consolidate-beliefs`) that keeps each
field's belief corpus coherent: it semantically de-duplicates currently-held
beliefs (block → match → merge, conservative bands with batch-API LLM
adjudication in the ambiguous middle), folding each duplicate's evidence onto a
canonical belief; and it ages stale beliefs (confidence decay + archival). Every
change is a coordinator-owned write that appends a `BeliefRevision` — **no belief
or revision row is ever deleted, no claim is touched.** A one-time CLI backfill
cleans the existing corpus; the scheduler runs it incrementally thereafter. The
job is **field-scoped** (`--field <slug>`, default `ai-robotics`) and never
merges or compares beliefs across fields.

---

## Principles (do not violate)

- **Coordinator-owned writes.** Every belief mutation runs under the
  `mesh_writer` role on the job process — same model as synthesis and the 16c
  consolidation job. No agent role gains write.
- **Field isolation (Phase 17).** Blocking, matching, merge, decay, and archival
  all filter by `field_id`. A cross-field belief merge is a correctness bug — the
  candidate query MUST take `field_id` exactly like `find_candidate_duplicates`.
- **Claims immutable.** No claim mutation anywhere. Merge re-points and unions
  *claim-id references*; it never edits claim content.
- **Beliefs are never deleted; revisions are append-only.** Migration 006
  deliberately withholds `DELETE` on `beliefs`/`belief_revisions`. **Honor that —
  this phase adds NO new DELETE grant.** A merged-away belief is marked
  `is_currently_held = false` and records a revision; it is absorbed, not erased.
  Every merge, decay, and archive writes a `BeliefRevision`.
- **Provenance mandatory.** A merge unions the duplicate's
  `supporting_claim_ids`/`contradicting_claim_ids` onto the canonical and records
  the absorbed belief id + the newly-added claim ids in the revision
  (`trigger_claim_ids`, `rationale`). No silent confidence changes.
- **Conservative, like entity resolution.** A false belief merge corrupts the
  knowledge base and is painful to unwind; a missed merge is cheap (caught next
  sweep). Auto-merge only the high band; auto-reject the low band; the middle
  band goes to the LLM and **defaults to not-same** on any ambiguity or parse
  failure.
- **No new LLM on the hot path.** Synthesis stays as-is except for cheap embedding
  population (a local fastembed call, not an LLM). All adjudication runs offline
  in the batch sweep with a sync fallback.
- **No new service or container.** The sweep is fired by the existing scheduler
  via a new `schedules` row + `JOB_COMMANDS` entry, exactly like
  `skeptic` / `memory_consolidation`.
- **Stamp a distinct agent identity.** Every revision this phase writes is
  attributed to `belief_consolidator` (`revised_by_agent`), never an existing
  agent's id.

---

## Scope

### 1. Belief embeddings — block 19a

Give beliefs the vector the blocking step needs, mirroring
`entities.name_embedding` exactly.

- Migration `011_belief_consolidation.sql` (011 is the next free number after
  `010_connectors.sql`): add
  `knowledge.beliefs.statement_embedding vector(384)` (nullable,
  reserved-then-populated, same as `name_embedding` was) and an HNSW
  `vector_cosine_ops` index. **No DELETE grant** — `mesh_writer` already holds
  `UPDATE` on `beliefs`; that is all merge needs. Note this in the migration
  comment as the deliberate contrast with 006.
- `mesh_llm.embeddings`: add `belief_embed_text(topic, statement) -> str`
  alongside `entity_embed_text` (the embedded text is `topic` + `statement`, the
  fields a near-duplicate would share). Reuse `EMBED_DIM` (384).
- `mesh_db.beliefs`: add `set_belief_embedding(conn, belief_id, vector)`
  mirroring `set_entity_embedding` (reuse the `_vector_literal` helper pattern
  from `mesh_db.entities`).
- Populate on write: in the coordinator `synthesize` node, after each
  `create_belief` / statement-changing `update_belief` (in `_run_sota` /
  `_run_capability`), embed `(topic, statement)` and `set_belief_embedding`.
  This is a local fastembed call — **not** an LLM, so the no-hot-path-LLM
  principle holds. Thread the existing `Embedder` already wired into the
  coordinator (`make_embedder()` at the entity-resolution call site); do not
  construct a second embedder.

**Exit:** migration applies cleanly; `statement_embedding` + HNSW index present;
new beliefs are written with a populated embedding; `ruff` + `mypy --strict`
clean; existing tests unaffected. Tag `v0.19.0-phase-19a`.

### 2. Belief-merge DB surface — block 19b

Add the block/choose/merge primitives to `mesh_db.beliefs`, mirroring the
`entities` trio but honoring the no-delete invariant and field isolation.

- `find_candidate_duplicate_beliefs(conn, embedding, *, k, exclude_id,
  field_id)` — pgvector nearest-neighbour over **currently-held**, **same-field**
  beliefs (`is_currently_held = true AND field_id = %s`), excluding the query
  belief itself, returning `(id, topic, statement, cosine_score)`. Mirror
  `find_candidate_duplicates`. Beliefs have no `type` to pre-filter on; block
  purely by vector neighbourhood (optionally restrict to a coarse belief family
  — `score` / `capability:` / relational — if the read shows those should never
  cross-merge; decide from the data and document the choice).
- `choose_canonical_belief(conn, id_a, id_b) -> (canonical_id, duplicate_id)` —
  mirror `choose_canonical`'s posture: keep the more-established belief (more
  supporting claims, then higher `revision_count`, then earliest
  `last_revised_at` / id as the deterministic tie-break). Document the ordering.
- `merge_beliefs(conn, canonical_id, duplicate_id) -> None` — a single
  transactional fold (`conn.raw.transaction()`), mirroring `merge_entities` but
  **without DELETE**:
  1. union `duplicate.supporting_claim_ids` into the canonical (set-dedup), same
     for `contradicting_claim_ids`;
  2. recompute the canonical's confidence from the enlarged evidence via
     `compute_confidence` (read `get_belief_signals` after the claim-id union, or
     recompute against the merged set — match how `_belief_confidence` does it);
  3. `create_revision` on the canonical — `previous_*`/`new_*` statement +
     confidence, `trigger_claim_ids` = the newly-folded claim ids,
     `revised_by_agent = "belief_consolidator"`, `rationale` naming the absorbed
     belief id;
  4. `update_belief` the canonical (claim-id unions, new confidence,
     `last_revised_at`, `revision_count + 1`);
  5. re-point any FK references to the duplicate belief (read for them —
     `investigations.opened_by_belief_id` / `resolution_belief_id`, any
     belief-anchored row) to the canonical via `UPDATE` (already granted; no
     DELETE);
  6. `create_revision` on the duplicate (`rationale` = "merged into
     `<canonical_id>`") and `update_belief` it to `is_currently_held = false`,
     `revision_count + 1`. The duplicate row and all its revisions remain for
     audit.

  Idempotent: if the duplicate is already not-held / already merged, return early
  (mirror `merge_entities`' "already merged / gone" guard).

**Exit:** merge folds a duplicate onto a canonical, appends revisions to both,
re-points references, leaves both rows present (duplicate not-held), never
deletes, never touches claim content, never crosses fields; unit-tested against
the testcontainer DB; `ruff` + `mypy --strict` clean. Tag `v0.19.0-phase-19b`.

### 3. Belief resolution agent — block 19c

Add `packages/mesh-agents/src/mesh_agents/belief_consolidation.py`, the belief
analog of `entity_resolution.py`.

- `BeliefMergeConfig` with high/low cosine bands from env
  (`MESH_BELIEF_MERGE_HIGH` default `0.95`, `MESH_BELIEF_MERGE_LOW` default
  `0.85` — start **tighter** than entity resolution's `0.93`/`0.80`; a false
  belief merge is costlier than a false entity merge). A `band(score) ->
  "merge"|"reject"|"adjudicate"` mapping mirroring the entity `band`.
- `adjudicate_beliefs(llm, belief_a, belief_b) -> bool` for the middle band — a
  structured LLM call asking "do these two beliefs assert the same proposition
  about the field?", **conservative**: any ambiguity, refusal, or
  `LLMResponseError`/parse failure returns `False` (not-same). Reuse the
  error-tolerant pattern from entity adjudication; a bad adjudication never
  merges.
- `resolve_belief_duplicates(conn, belief, *, embedder, llm, config) ->
  list[MergeDecision]` — block (`find_candidate_duplicate_beliefs`) → band → for
  high return merge, for low return reject, for middle adjudicate. Returns
  decisions; **does not write** (the job applies them), so the agent stays
  write-free per the role model.

**Exit:** given two near-identical beliefs the agent returns a high-band merge;
given unrelated beliefs, reject; the middle band invokes adjudication and
defaults not-same on failure; unit-tested with a mock `LLMClient` + a stub
embedder; `ruff` + `mypy --strict` clean. Tag `v0.19.0-phase-19c`.

### 4. Consolidation graph + schedule + CLI — block 19d

Create the sweep as a LangGraph graph cloned from `apps/pipeline/consolidation.py`
(itself cloned from `skeptic_sweep.py` — match its `open_checkpointer`,
traceparent propagation, batch/sync structure, `pipeline_run_exists` finalize
guard, and Langfuse cost attribution). Job:
`apps/pipeline/src/mesh_pipeline/belief_consolidation.py`.

- Iterate **active fields** (mirror `consolidation.py`'s `load_history` listing
  active fields) and, per field, select candidates: currently-held beliefs,
  prioritising recently-revised / recently-embedded ones (so the sweep is
  incremental, not a full O(n²) re-scan every run; document the bound and `log`
  what was skipped).
- For each, `resolve_belief_duplicates`; collect high-band pairs and middle-band
  pairs. Run the **batch-API** LLM adjudication for the middle band (mirror the
  `MESH_CONSOLIDATION_BATCH` / `MESH_SKEPTIC_BATCH` pattern with a working sync
  fallback). Model via env routing
  (`MESH_LLM_MODEL_BELIEF_CONSOLIDATOR` → default via `resolve_model`) — do not
  hardcode. (If Phase 20 routing has landed, this agent slots into the cheap
  tier with escalation; it must still work without it.)
- Apply confirmed merges through `merge_beliefs` under the coordinator-writer
  connection. De-dup decisions so A↔B isn't applied twice in one run; never merge
  a belief that was already absorbed earlier in the same run.
- Entry point `mesh-consolidate-beliefs =
  mesh_pipeline.belief_consolidation:main` in `apps/pipeline/pyproject.toml`.
- Scheduler: add `belief_consolidation` to `DEFAULT_INTERVALS` (default **24h**)
  in `mesh_a2a.schedules`, and
  `JOB_COMMANDS["belief_consolidation"] = ["uv","run","mesh-consolidate-beliefs"]`
  in `apps/scheduler/.../scheduler.py`. No new container — the scheduler
  registers it from the `schedules` table per field automatically.
- `Makefile`: add a `consolidate-beliefs` target mirroring `consolidate-memory`
  (`docker compose run --rm --no-deps --entrypoint "uv run
  mesh-consolidate-beliefs" …` on the coordinator/skeptic-sweep image).
- One-time backfill + dry-run report in `apps/cli`, mirroring
  `reconcile-entities` / `mesh_agents.reconcile`: `mesh.cli consolidate-beliefs
  [--field <slug>] [--apply] [--report-path …] [-k …]` — first backfills
  `statement_embedding` for any belief with a NULL embedding (mirror
  `reconcile`'s NULL-embedding backfill via the Batch API), then
  reports/optionally-applies merges across the existing corpus. Read-only by
  default (`--apply` to write).

**Exit:** `make consolidate-beliefs` runs end-to-end and merges ≥1 duplicate on a
seeded corpus (or cleanly no-ops on a clean one), appending revisions and leaving
the duplicate not-held; batch path used with a working sync fallback; cost
attributed in Langfuse; no hot-path LLM added; `mesh.cli consolidate-beliefs`
produces a report; `ruff` + `mypy --strict` clean. Tag `v0.19.0-phase-19d`.

### 5. Staleness decay & archival — block 19e

A second, **LLM-free** pass in the same job that ages the corpus — cheap, time-
and-evidence-based, append-only. (Deliberately narrow: confidence decay +
archival only, *not* a belief-lifecycle redesign — see Out of Scope.)

- Decay: for currently-held beliefs whose `last_revised_at` is older than a
  configurable half-life (`MESH_BELIEF_DECAY_HALFLIFE_DAYS`, default `90`),
  multiply `confidence` by the age-derived decay factor, floored at a
  configurable minimum (`MESH_BELIEF_DECAY_FLOOR`, default `0.1`). Write a
  `BeliefRevision` (rationale = "staleness decay", `revised_by_agent =
  "belief_consolidator"`, statement unchanged).
- Archive: a belief not revised for longer than
  `MESH_BELIEF_ARCHIVE_AFTER_DAYS` (default `365`) **and** unsupported by any
  live claim → `is_currently_held = false` with a revision (rationale = "archived:
  stale, no live evidence"). **No delete.** Archived beliefs simply drop out of
  the held set (and thus out of the wiki's default views, which already filter on
  `is_currently_held`).
- Reuse `find_stale_beliefs` for candidate selection where it fits; extend it
  only if the age/evidence predicate it lacks is needed (don't duplicate it). All
  selection is field-scoped.

**Exit:** on a seeded corpus, a stale belief's confidence decays with a recorded
revision and a long-dead unsupported belief is archived (not-held) with a
revision; no belief or revision row deleted; no LLM used in this pass; `ruff` +
`mypy --strict` clean. Tag `v0.19.0-phase-19e`.

### 6. CLI inspection surface

Add `mesh.cli beliefs duplicates [--field <slug>]` (read-only; mirror
`heuristics list` / `investigations list`): list candidate duplicate pairs above
the low band with their cosine score and band, so pending merges can be eyeballed
without running the sweep. (The `consolidate-beliefs --report-path` from 19d is
the batch report; this is the quick interactive view.)

### 7. Docs

Add `docs/belief-consolidation.md` covering the block → match → merge → decay
pipeline, the no-delete / append-only contrast with entity merge, the
conservative bands + adjudication default, field isolation, the sweep cadence,
and the one-time backfill. Match the existing `docs/` style (e.g.
`docs/entity-resolution.md`, `docs/belief-synthesis.md`). Update `CLAUDE.md`'s
phase-status paragraph and the environment-variable table with the new
`MESH_BELIEF_*` knobs.

---

## Out of Scope (do not build)

- **Cross-belief contradiction detection / routing.** Finding *pairs* of beliefs
  that conflict (vs. the Skeptic attacking one belief) is a separate design — it
  must not double-count the Skeptic's existing counter-claims. Future phase.
- **Thematic / narrative synthesis** ("what's going on in the industry" as trends
  above atomic beliefs). That read-only derived layer is the Knowledge Chatbot
  (Phase 21) and Autonomous Discovery (Phase 22); it must never write back into
  beliefs.
- **A full belief-lifecycle / single-state-evaluation redesign.** This phase uses
  per-belief decay + archival only; no unified state machine.
- **DSPy / prompt optimization; any new service or container; any new CI job.**
- **Wiki routes/UI changes.** Archived/merged beliefs already fall out of held
  views via `is_currently_held`; no UI work this phase (Phase 23 surfaces the
  `belief_consolidator`'s activity in the agent-observability view).
- **Claim mutation; any DELETE grant on beliefs/revisions; any role gaining write
  beyond the existing `mesh_writer`.**
- **Cross-field comparison or merge.** Field isolation is absolute.

---

## Exit Criteria

- [ ] Migration `011` applies cleanly; `beliefs.statement_embedding vector(384)`
      + HNSW cosine index present; **no new DELETE grant** added
- [ ] `belief_embed_text` + `set_belief_embedding` added; new beliefs written with
      a populated embedding from the `synthesize` node (local fastembed, no LLM)
- [ ] `find_candidate_duplicate_beliefs` (field-scoped, held-only),
      `choose_canonical_belief`, and a transactional `merge_beliefs` exist; merge
      folds claim-id unions onto the canonical, recomputes confidence, appends a
      revision to **both** beliefs, re-points references, and marks the duplicate
      `is_currently_held = false` **without deleting any row**
- [ ] `belief_consolidation.py` agent returns merge/reject/adjudicate by band;
      the middle band adjudicates via LLM and **defaults to not-same** on any
      failure; agent performs no writes
- [ ] `make consolidate-beliefs` runs end-to-end per active field; merges
      confirmed duplicates; uses the batch API with a working sync fallback; cost
      attributed in Langfuse; no hot-path LLM added
- [ ] Stale beliefs decay (confidence ↓, revision recorded) and long-dead
      unsupported beliefs are archived (not-held, revision recorded) — LLM-free,
      no deletes
- [ ] `mesh.cli consolidate-beliefs` (backfill + report/apply) and
      `mesh.cli beliefs duplicates` (read-only view) work, both `--field`-aware
- [ ] `docs/belief-consolidation.md` added; `CLAUDE.md` phase status + env table
      updated
- [ ] `ruff` + `mypy --strict` clean across all touched packages; existing pytest
      + Playwright suites unaffected
- [ ] Coordinator-owned writes preserved; field isolation preserved; claims
      unmodified; no belief/revision deletes; no new service; no role relaxation;
      every change attributed to `belief_consolidator` and provenance-grounded

---

## Commit Convention

One logical commit per unit; conventional messages:

```
feat(db): add beliefs.statement_embedding + HNSW index (migration 011)
feat(llm,db): add belief_embed_text + set_belief_embedding; populate on synthesis
feat(db): add belief block/choose/merge surface (no-delete, append-only)
feat(agents): add belief_consolidation resolver + conservative adjudication
feat(pipeline): add belief-consolidation graph + schedule + make target
feat(cli): add consolidate-beliefs backfill/report + beliefs duplicates view
feat(pipeline): add staleness decay + archival pass
docs: add belief-consolidation.md; update CLAUDE.md
```

Tags map to blocks: `v0.19.0-phase-19a` (embeddings), `…-19b` (merge surface),
`…-19c` (resolver), `…-19d` (sweep + schedule + CLI), `…-19e` (decay + archival).
Execute 19a → 19e straight through; no gate between blocks; lint, types, and a
clean run are the only bar before each tag. Report any principle conflict (e.g. a
belief-id FK that can't be cleanly re-pointed without a DELETE) before working
around it.
