# Derived signals

Phase 7b added three derived views over the existing claims + sources
+ revisions data. The views are recomputed on read — no stored
columns, no materialization. Informational only — nothing in the
pipeline reads them to gate behavior.

## Views

### `belief_reproduction`

Per currently-held belief, the **max distinct source types backing
any single canonical claim** attached to it. Canonical key follows
the rule from `docs/reproduction-signal-exploration.md`:

- `achieves_score` / `outperforms` / `evaluated_on` predicates round
  numeric scores to one decimal place and lowercase the benchmark
  name. "78.42 on MMLU" and "78.38 on MMLU" collide; "78.4 on MMLU"
  and "61.0 on HellaSwag" don't.
- `developed_by` predicate lowercases the organization string.
- Everything else uses the raw JSON.

The signal answers "did multiple kinds of sources independently say
this?" — three different source types (arxiv + leaderboard + blog)
reporting the same score is a stronger signal than one paper saying
it three times.

### `belief_signals`

Raw inputs to the hype/substance score, exposed individually so the
UI can show *why* a score is what it is, not just the final number.
Columns: `source_type_diversity`, `reproduction_count`,
`skeptic_counter_claim_count`, `severe_failure_mode_count`
(methodological_flaw + cherry_picked_evidence +
contradicted_by_source), `claims_last_30d`.

### `belief_hype_substance`

Single 0-1 score per belief.

```
substance = ( min(source_diversity/4, 1) + min(reproduction/3, 1) ) / 2 * 0.5
hype      = ( min(attacks/4, 1)          + min(severe/3, 1) )       / 2 * 0.5
score     = clamp(0.5 + substance - hype, 0, 1)
```

**The 0.5 anchor is load-bearing.** A belief with zero supporting
evidence AND zero attacks isn't substantive *and* isn't hype — it's
informational nothing. The score sits at 0.5 for that case, not 0.

A belief with diverse + reproduced evidence and no attacks tops out
at 1.0. A belief with no supporting evidence and multiple severe
attacks bottoms out at 0.0. Mixed beliefs land in between.

Equal weighting on substance vs hype (0.5 each) keeps the formula
symmetric so the score reads as a balance, not a leaderboard.

## Wiki surfaces

- **`/beliefs/[id]` detail page**: shows `BeliefSignalsCard` between
  the header and the supporting/contradicting grid. Hype↔substance
  number with bucket label (hype-shaped / mixed signal /
  substantive), each individual signal with its hint, anchor note.
- **`/beliefs/[id]/timeline`**: full revision history with an inline
  SVG step-chart of confidence over time. Skeptic challenges colored
  destructive, others primary. Hover for exact timestamp + agent.
- **`/graph`**: cytoscape.js view of all entities (nodes, colored by
  type) and relationships (edges, labeled). Filter chips toggle
  entity types on/off. Click a node → entity page.

## API surface

- `GET /api/v1/beliefs/{id}` includes a `signals` field (null for
  superseded beliefs since the views filter to held-only).
- `GET /api/v1/graph?max_nodes=&max_edges=` returns
  `{nodes: GraphNode[], edges: GraphEdge[]}`. Edges to non-included
  nodes are dropped server-side.

## What's not here

- These scores don't gate anything in the pipeline. If you want a
  belief skipped because its hype-substance is below X, that's a
  Phase 8+ change.
- No score caching. Postgres views recompute on each query; the math
  is cheap and the mesh is single-user.
- No claim-level scoring. The signal lives at the belief level
  because beliefs are the unit of "what do we think is true."
