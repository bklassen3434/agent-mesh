# Belief Synthesis (Phase 14)

Before Phase 14 the mesh scouted the whole frontier but only formed **beliefs**
from leaderboard/SOTA scores. Every other extracted claim — capabilities,
comparisons, attributions, lineage, reproductions, critiques — sat in the
`claims` table with no belief or edge referencing it, so the wiki, graph, and
briefings (which render *beliefs* and *edges*) never surfaced it.

Phase 14 generalizes synthesis: every claim is **typed**, a type-routed
synthesis step turns claims into the right artifact, and beliefs carry **real
confidence** computed from their evidence.

## 14a — Typed claims

Every claim carries a `claim_type` (`mesh_models.claim.ClaimType`): one of
`score, capability, comparison, attribution, lineage, evaluation, reproduction,
critique, speculative`. It is **derived 1:1 from the predicate**
(`PREDICATE_TO_CLAIM_TYPE`) — classification metadata, not new content, so claim
immutability holds. A `model_validator` fills it whenever a caller omits it; an
unknown predicate falls back to the inert `speculative` bucket (never
synthesized).

The extractor learned five new predicates (`has_capability`, `based_on`,
`reproduces`, `critiques`, `speculates`) with their object schemas + worked
examples, so it can finally emit the non-leaderboard shapes.

Migration `007_claim_type.sql` adds the column (CHECK-constrained to the enum),
deterministically backfills existing claims from their predicate, and indexes
it. The backfill is deterministic (not an LLM pass) because pre-Phase-14 claims
only ever carried the four legacy predicates, which map cleanly to a type.
`mesh.cli backfill-claim-types` re-applies the map idempotently.

## 14b — Type-routed synthesis

`apps/pipeline/coordinator.py` `synthesize` node dispatches on `claim_type`:

- **`score`** → the unchanged SOTA handler (`sota_tracker.update_sota_pure`);
  leaderboard output is byte-for-byte identical.
- **`capability`** → an **entity-anchored belief** keyed `capability:<entity_id>`
  (`mesh_agents.synthesis`). The coordinator rebuilds it from the entity's
  *full* active capability claim set each run, so all capability claims about
  one Phase-13 canonical entity converge on a single belief with complete
  provenance. Idempotent: no new evidence → no revision.
- **`reproduction` / `critique`** are not standalone beliefs (they are evidence
  signals — see 14d). **`speculative`** is stored but not synthesized.

Belief writes stay coordinator-owned (single-writer discipline).

## 14c — Relational claims become edges

Relational claim types route to a small fixed edge vocabulary, written to the
`relationships` table:

| claim_type   | edge          | target key (in claim object) |
|--------------|---------------|------------------------------|
| comparison   | `outperforms` | `compared_to`                |
| attribution  | `developed_by`| `lab`                        |
| lineage      | `based_on`    | `parent`                     |
| evaluation   | `evaluated_on`| `benchmark`                  |

The coordinator resolves the *target* entity named in the claim object so both
endpoints are real canonical nodes, then synthesizes edges on the main path.
Edges are claim-grounded; repeat assertions of the same `(from, to, type)`
aggregate onto one edge (`relationships.add_relationship_evidence` dedups
evidence and lifts confidence to the strongest supporting claim). Self-loops and
unresolved targets are skipped, not fabricated. `graph.graph_edges` already
reads the table, so `/graph` now renders real edges.

## 14d — Confidence from signals

`mesh_agents.confidence.compute_confidence` replaces the hardcoded `0.5` on every
synthesized belief (score and capability alike). It maps the belief's evidence
signals — read from the `belief_signals` view after the claim links are written
— to a confidence in `[0, 1]`:

```
support = (sat(source_diversity) + sat(reproduction_count)) / 2
attack  = (sat(skeptic_counter_claims) + sat(severe_failure_modes)) / 2
confidence = clamp(base + support_weight*support - attack_weight*attack, 0, 1)
```

Weights live in **config** (`ConfidenceWeights.from_env`, `MESH_CONFIDENCE_*`),
not buried in code, so a later calibration phase can tune them. The defaults are
hand-set to reproduce the `belief_hype_substance` formula; this phase does **not**
fit them against outcomes. Confidence remains a mutable belief field; the Skeptic
still adjusts it later via `suggested_confidence_delta`.

> Note: extracted `reproduction`/`critique` claims move confidence only once
> they are attached to a belief's supporting/contradicting arrays. Skeptic
> critiques are already attached and lower confidence today; attaching
> *extracted* reproduction/critique claims to the belief they bear on needs
> claim→belief clustering, which is explicitly deferred (out of Phase 14 scope).
