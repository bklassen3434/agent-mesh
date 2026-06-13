# Belief Consolidation (Phase 19)

## Why

Three maintenance mechanisms already keep parts of the mesh tidy — entity
resolution cleans the graph's *nodes* (Phase 13), the memory-consolidation job
distills the agents' *know-how* (Phase 16c), and belief synthesis writes the
*facts* (Phase 14). Nothing cleaned **the beliefs themselves.**

`score` (`sota:*`) and `capability:<entity_id>` beliefs converge deterministically,
but two failure modes accumulate duplicates anyway: beliefs synthesized under
different `topic` strings that assert the same proposition, and capability
beliefs anchored on two entities that were only *later* merged. And beliefs never
aged — a SOTA belief from eight months ago stayed `is_currently_held = true` at
full confidence forever. `find_stale_beliefs` *flagged* staleness for the Curator,
but nothing consolidated or aged the corpus.

Phase 19 is the **world-model analog of entity resolution**, packaged in the
**job shape of the Phase 16c consolidation graph**: a scheduled batch sweep that
semantically de-duplicates beliefs (block → match → merge), then ages stale ones
(decay + archival).

## The hard invariant: append-only

Entity merge is allowed to **delete** the absorbed duplicate row (migration 006
grants `mesh_writer` `DELETE` on `entities`/`relationships`). Belief consolidation
is the opposite — it is strictly **append-only**:

- A merged-away belief is marked `is_currently_held = false` and keeps **all** its
  revisions for audit. No belief or revision row is ever deleted.
- Migration 011 deliberately adds **no `DELETE` grant**. `mesh_writer` already
  holds `UPDATE` on `beliefs`; that is all merge, decay, and archival need.
- Claims are never touched. A merge unions the duplicate's *claim-id references*
  onto the canonical; it never edits claim content.
- Every merge, decay, and archive appends a `BeliefRevision` attributed to
  `belief_consolidator` (never an existing agent's id).

This mirrors the claim-immutability / revision-append-only invariants and keeps
them enforced at the DB level, exactly as for claims.

## Pipeline: block → match → merge

### Block (`mesh_db.beliefs.find_candidate_duplicate_beliefs`)

Each belief carries a `statement_embedding vector(384)` (pgvector, HNSW cosine
index — migration 011), populated on synthesis from `topic` + `statement` via
`mesh_llm.belief_embed_text` (a local `fastembed` call — **not** an LLM, so the
no-hot-path-LLM principle holds). The block query returns the top-k nearest
**currently-held**, **same-field** beliefs by cosine distance, excluding the
query belief.

Beliefs have no `type`, so blocking restricts candidates to the same coarse
**family** instead: `score` (`sota:*`) and `capability` (`capability:*`). The two
families assert different kinds of proposition and must never cross-merge, even
if their embeddings drift close.

### Match (`mesh_agents.belief_consolidation`)

Three bands on cosine similarity (`1 - distance`), config-tunable:

| band | rule | action |
|---|---|---|
| high | `>= MESH_BELIEF_MERGE_HIGH` (0.95) | auto-merge |
| middle | between | LLM adjudication |
| low | `<= MESH_BELIEF_MERGE_LOW` (0.85) | auto-reject |

The bands start **tighter** than entity resolution's (0.93 / 0.80): a false
belief merge corrupts the knowledge base and is painful to unwind, while a missed
merge is cheap (caught next sweep). The middle band asks the LLM "do these two
beliefs assert the same proposition about the field?" and **defaults to not-same**
on any ambiguity, refusal, or parse failure. With no LLM available, the middle
band rejects.

### Merge (`mesh_db.beliefs.merge_beliefs`)

A single transactional, append-only fold of a duplicate onto a canonical
(`choose_canonical_belief` keeps the more-established belief: more supporting
claims → higher `revision_count` → earliest `last_revised_at` → smaller id):

1. union the duplicate's `supporting_claim_ids` / `contradicting_claim_ids` onto
   the canonical (set-dedup);
2. recompute the canonical's confidence from the enlarged evidence
   (`belief_signals` → `compute_confidence`, injected as a `ConfidenceFn` so
   `mesh_db` stays free of the `mesh_agents` dependency);
3. re-point belief FK references (`investigations.opened_by_belief_id` /
   `resolution_belief_id`) duplicate → canonical;
4. append a `BeliefRevision` to the canonical (trigger = the newly-folded claim
   ids, rationale naming the absorbed belief);
5. mark the duplicate `is_currently_held = false` and append its own merge
   revision.

Idempotent: a no-op if the duplicate is already not-held. Belief-row updates run
*before* their revisions are appended (the `belief_revisions → beliefs` FK rejects
updating a row already referenced by a freshly-inserted revision in the same tx).

## Staleness decay + archival (LLM-free)

A second pass in the same job ages the held corpus — cheap, time-and-evidence
based, append-only. Deliberately narrow: decay + archival only, not a
belief-lifecycle redesign.

- **Decay:** a held belief not revised for longer than the half-life
  (`MESH_BELIEF_DECAY_HALFLIFE_DAYS`, 90) has its confidence multiplied by
  `0.5 ** (age / halflife)`, floored at `MESH_BELIEF_DECAY_FLOOR` (0.1). Records a
  "staleness decay" revision (statement unchanged).
- **Archive:** a held belief not revised for longer than
  `MESH_BELIEF_ARCHIVE_AFTER_DAYS` (365) **and** unsupported by any live (active)
  claim is flipped `is_currently_held = false` with an "archived: stale, no live
  evidence" revision. Archival takes precedence over decay for the same belief.
  Archived beliefs simply drop out of the held set (and thus the wiki's default
  views, which already filter on `is_currently_held`) — **no delete.**

## Field isolation

Blocking, matching, merge, decay, and archival all filter by `field_id`. A
cross-field belief merge is a correctness bug — the candidate query takes
`field_id` exactly like `find_candidate_duplicates`. The sweep iterates active
fields and never compares or merges across them.

## The sweep + cadence

`apps/pipeline/.../belief_consolidation.py` is a checkpointed LangGraph job cloned
from the Phase 16c consolidation graph:

```
START → load_candidates ─[middle pairs?]→ submit_batch | adjudicate_sync | decay
  submit_batch → poll_batch → collect_results → decay
  decay → finalize → END
```

`load_candidates` (per active field) backfills missing embeddings, blocks +
bands the candidate set, applies high-band merges immediately, and stages the
middle band. Middle-band adjudication runs through the **Anthropic Batch API**
(50% cheaper) by default, with a synchronous fallback for other providers. The
model is env-routed for the `belief_consolidator` role
(`MESH_LLM_MODEL_BELIEF_CONSOLIDATOR` → `resolve_model` default); batched
generations are traced to Langfuse and ledgered in `llm_usage`. The `finalize`
node is idempotency-guarded (`pipeline_run_exists`).

Incrementality: each run scans at most `MESH_BELIEF_CANDIDATE_LIMIT` (500)
most-recently-revised held beliefs per field (blocking still searches the full
held set), and logs what it skipped — so the sweep is not a full O(n²) re-scan
every run.

The existing scheduler fires it daily (`belief_consolidation`, 24h default in
`DEFAULT_INTERVALS` + `JOB_COMMANDS`) — **no new service or container.**

## The one-time backfill

`mesh.cli consolidate-beliefs [--field <slug>] [--apply] [--report-path …] [-k …]
[--no-decay]` backfills `statement_embedding` for any held belief missing one,
then reports (or, with `--apply`, performs) merges + decay across the existing
corpus. Read-only by default; mirrors `reconcile-entities`. Idempotent.

`mesh.cli beliefs duplicates [--field <slug>]` is the quick interactive view: it
lists candidate duplicate pairs above the low band with their cosine score and
band, so pending merges can be eyeballed without running the sweep.

## Out of scope

Cross-belief contradiction detection, thematic/narrative synthesis, a unified
belief-lifecycle state machine, and any wiki/UI changes are explicitly out of
scope (later phases). Archived/merged beliefs already fall out of held views via
`is_currently_held`.
