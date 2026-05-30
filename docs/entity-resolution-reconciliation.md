# Entity Resolution — Reconciliation Report

This file is **generated** by `uv run mesh.cli reconcile-entities` (Phase 13c).
Each run overwrites it with before/after counts, merge tallies, and a sample of
merges for manual false-merge review.

## How to run

```bash
# 1. Ensure every entity has an embedding (idempotent).
uv run mesh.cli backfill-entity-embeddings

# 2. Dry run — compute and report planned merges, write nothing.
uv run mesh.cli reconcile-entities                 # writes this file

# 3. Review the "Sample of merges" below for false merges. If any genuinely
#    different entities were collapsed, raise the auto-merge threshold and re-run:
#       export MESH_ENTITY_MERGE_HIGH=0.95
#    A missed merge is cheap (caught next pass); a false merge is not.

# 4. Apply for real once the sample looks clean.
uv run mesh.cli reconcile-entities --apply
```

Middle-band adjudications route through the Anthropic Batch API and need
`ANTHROPIC_API_KEY`. Without it, only high-confidence (high-band) duplicates are
auto-merged and ambiguous pairs are left in place (conservative).

> **Production run pending.** This must be run against the live knowledge store
> (the accumulated `knowledge.entities` table) in the deployment environment;
> that database is not available from the dev checkout. Run the steps above and
> commit the regenerated report.

---

## Validation run (seeded sample data)

To verify the command end-to-end, a fresh `pgvector/pgvector:pg16` container was
seeded with known duplicate clusters and reconciled with the **real** fastembed
embedder (`llm=None`, so high-band auto-merges only — no API key):

| metric | value |
|---|---|
| Entities before | 10 |
| Entities after | 8 |
| Merges (duplicates absorbed) | 2 |
| Auto-merges (high band) | 3 pairs |
| Middle-band pairs (would go to LLM) | 7 |

**Outcome — behaves as designed:**

- `ImageNet` (benchmark) ← `ImageNet-1k`, `ImageNet 1K` — the benchmark cluster
  collapsed to one canonical node with the variants recorded as aliases.
- `ImageNet` (model) was **not** merged into `ImageNet` (benchmark) despite the
  identical name — blocking is type-filtered, so cross-type namesakes never pair.
- The `Mamba` / `Mamba-2` / `Mamba (SSM)` / `the Mamba architecture` variants
  (cosine ~0.88–0.92, middle band) were **left unmerged** because no LLM was
  available — the conservative bias in action. With `ANTHROPIC_API_KEY` set they
  would be adjudicated and, if confirmed, collapsed.
- A second `--apply` run reported **0 merges / 0 new embeddings** — idempotent.

This is a synthetic validation of the mechanism, not a report of the production
entity table; the production run replaces the section above.
