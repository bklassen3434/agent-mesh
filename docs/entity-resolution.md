# Entity Resolution (Phase 13)

## Why

Entities used to be deduplicated by **exact string match** only. "Mamba",
"Mamba-2", "Mamba (SSM)", and "the Mamba architecture" each became a separate
node, fragmenting the claims, edges, and beliefs that should accumulate on one
entity. Every graph reader (node ranking, belief synthesis, trend signals) then
saw a wrong, splintered picture. Phase 13 replaces exact-match with **semantic
resolution** so duplicates collapse onto a single canonical node — both as a
one-time cleanup and as a guard on the live ingestion path.

## The hard invariant

A merge consolidates **entity identity only**. It never mutates claim content
(`predicate` / `object` / `raw_excerpt`). Claims stay immutable; resolution is a
mutable reference layer *over* them. A merge re-points the FK
`claims.subject_entity_id` (and relationship/investigation references) — it does
not rewrite claims. This is enforced at the DB level: `mesh_writer` has no
`DELETE`/`UPDATE`-of-content path that would let it alter claim text, and the
merge's only claim write is the `subject_entity_id` re-point.

## Pipeline: block → match → merge

### Block (`mesh_db.entities.find_candidate_duplicates`)
Each entity has a `name_embedding vector(384)` (pgvector, HNSW cosine index).
The embedding text is normalized as `"{name} ({type})"` via
`mesh_llm.entity_embed_text` — backfill, reconciliation, and the live path all
use that one function so blocking stays consistent. A candidate query returns
the top-k nearest entities by cosine distance, **filtered by entity type** (a
model never blocks against a benchmark) and excluding the entity itself.

**Embedding backend:** `fastembed` (ONNX, `BAAI/bge-small-en-v1.5`, 384-dim) —
arm64-native, no torch, offline once cached. Selected via `MESH_EMBED_MODEL`.
Behind the `Embedder` Protocol so tests inject deterministic vectors.

### Match (`mesh_agents.entity_resolution`)
Three bands on cosine similarity (`1 - distance`), all config-tunable:

| band | rule | action |
|---|---|---|
| `similarity >= high` (`MESH_ENTITY_MERGE_HIGH`, default **0.93**) | confident | auto-merge |
| `similarity <= low` (`MESH_ENTITY_MERGE_LOW`, default **0.80**) | clearly different | auto-reject |
| in between | uncertain | LLM adjudication |

Only the middle band hits the LLM (cost discipline). The adjudicator is given
both names, types, aliases, and a few representative claims, and **defaults to
`same_entity=False`** on any parse failure or uncertainty.

**Conservative bias.** A false merge corrupts provenance and is painful to
unwind; a missed merge is cheap (caught next pass). So thresholds favor leaving
duplicates, and the LLM defaults to "not the same." Empirically (bge-small),
near-duplicate name variants score ~0.88–0.93 and unrelated entities ~0.5, so
the default bands route most genuine variants to the LLM rather than
auto-merging them.

### Merge (`mesh_db.entities.merge_entities`)
Consolidates duplicate **B** into canonical **A** in a single transaction:

1. **Canonical choice** (`choose_canonical`): most-claimed wins; tie-break
   earliest `created_at`, then smaller `id`.
2. Re-point `claims.subject_entity_id`, relationship endpoints, and investigation
   `target_entity_id` / `related_entity_ids` from B to A.
3. **Edge aggregation:** relationships that now collide on `(from, to, type)` are
   merged (union `evidence_claim_ids`, max `confidence`); canonical self-loops the
   merge created are dropped.
4. Fold B's `canonical_name` + aliases into `A.aliases` (case-insensitive dedup).
5. Delete B.

Aliases preserve B's surface forms, so future string references to B resolve to
A via the alias fast-path. All writes are coordinator/writer-owned.

## Live path (`resolve_entity_semantic`)

Per candidate name during ingestion:
1. **Alias/exact fast-path** (string match on canonical name or aliases) → attach,
   **no embed, no LLM**. Absorbs the overwhelming majority of repeat references.
2. Else embed, block (type-filtered), classify the nearest candidate.
3. high → attach; low → create new; middle → LLM adjudicate.
4. On attach, record a novel surface form as an alias (so the string fast-path
   catches it next time). On create, persist the new entity with its embedding.

## Reconciliation (one-time)

`mesh.cli reconcile-entities` sweeps the whole table with the same block → match
→ merge logic, clustering confirmed-same pairs with union-find and merging each
cluster onto one canonical. Middle-band adjudications route through the Anthropic
**Batch API** (50% cheaper) when available. `--apply` performs merges; the
default is a dry run. A report is written to
`docs/entity-resolution-reconciliation.md` for false-merge review. Idempotent:
merged duplicates are deleted, so a second run finds little to do. See that file
for methodology and results.

## Out of scope (Phase 13)
Belief synthesis / claim typing, edge population from claims, belief lifecycle /
ranking, cross-type merges (blocking is type-filtered), and any mutation of claim
content. Those are later phases.
