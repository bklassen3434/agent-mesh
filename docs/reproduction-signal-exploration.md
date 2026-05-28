# Reproduction signal — exploration notes

Phase 7 pre-work. Quick analysis to inform the 7b reproduction-tracker
view design. Run via:

```bash
uv run python scripts/explore_reproduction.py
```

## What the script does

For every `(subject_entity_id, predicate, object_key)` triple in
`claims`, count how many distinct `sources.type` values corroborate it.
The `object_key` is a coarse-by-design canonical form (see
`_object_key()` in the script) so paraphrased counter-claims and small
numeric noise don't shatter the match.

Canonicalization rules:

- `achieves_score` / `outperforms` / `evaluated_on`: round numeric
  scores to one decimal place and lowercase the benchmark name.
- `developed_by`: lowercase organization/developer string.
- Everything else: sorted JSON of the full object.

## Findings (run against the current local DB)

The dev DB is essentially empty (`pipeline_runs.claims = 0`) so the
script produces no data. Findings will be updated after the first
populated `make pipeline` run.

For the 7b design we proceed with the canonicalization shape above as
the default tolerant-match. If a populated run shows the rule is too
coarse (e.g. distinct benchmark sub-tasks colliding) or too tight (e.g.
0.1% scoring deltas splitting what's morally the same result),
re-balance the rounding constant in `_object_key()` before locking in
the view.

## How the 7b reproduction view will use this

The view `belief_reproduction` will, per held belief:

1. Project the supporting + contradicting claims through `_object_key()`
   to get canonical (subject, predicate, object_key) tuples per source
   type.
2. Take the MAX count of distinct source types backing any single
   canonical tuple. This is the belief's reproduction count — a single
   well-corroborated claim is worth more than many incidental mentions.
3. The hype/substance score in 7b reads this view directly. Higher
   reproduction → higher substance weight.

The `_object_key()` rule lives in `mesh_db` as a SQL UDF (DuckDB
supports `CREATE FUNCTION` over Python) so view + Python script share
one source of truth. Decision deferred to 7b implementation; flagging
here so we don't drift.

## Open questions for 7b

- Should `developed_by` and `achieves_score` contribute equally to
  reproduction? Probably not — a model being developed by one org is
  a fact one source can establish; a benchmark score being reproduced
  is the real signal.
- Tolerance for "same score": ±0.1 numeric, lowercase benchmark name.
  Revisit if the next populated run shows mis-clustering.
- Do we filter to currently-held beliefs only, or include superseded
  ones for historical analysis? Current-held only for the score; the
  full history can be a separate view if useful.
