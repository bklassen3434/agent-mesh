# Reasoning tiers

Every tension routes 1:1 to a skill (`Tension.handler_skill`). The **reasoning
tier** adds a second, orthogonal dimension: *how much reasoning that tension is born
needing*. It is a property of the kind (with signal-driven upgrades), stamped at
tension production and read by the rule engine to decide how the skill is
dispatched.

The dividing line is one question: **is the information needed to answer already on
the board?**

| Tier | When | Mechanism |
|---|---|---|
| **simple** | The answer is already in front of the skill. | One dispatch, `fanout = 1`. |
| **swarm** | One answer, but the path to it is noisy — a single LLM pass is unreliable. | `fanout = K` (`MESH_CONTROLLER_SWARM_SIZE`) **from the first dispatch**; the K copies' effects are reconciled (union, or a `MESH_CONTROLLER_SWARM_QUORUM` majority vote). |
| **deep** | The answer needs evidence the board doesn't have yet. | A *plan → gather → reason → decide* loop that unfolds **across controller rounds** — the skill advances a small state machine on the board; the controller's re-sense is the loop. `fanout = 1`. |

`ReasoningTier` lives in `mesh_models.tension`; the per-kind defaults and the
upgrade function (`resolve_tier`) live in `mesh_agents.agenda`.

## The board → tier map

| TensionKind | Handler skill | Tier | Why |
|---|---|---|---|
| `unscouted_connector` | `scout-source` | simple | one fetch, idle+cooldown gated |
| `unextracted_source` | `extract-source` | simple | text → claims, one shot |
| `unsynthesized_claims` | `synthesize-belief` | simple | claims → belief summarize |
| `merge_candidate` | `merge-candidate` | simple → **swarm** in the gray band | high/low similarity is deterministic; the ambiguous middle band is worth K adjudicators |
| `redundant_beliefs` | `consolidate-beliefs` | simple → **swarm** in the gray band | same banding, belief thresholds |
| `missing_reciprocal_edge` | `investigate-gap` | simple | add the reciprocal edge |
| `aging_belief` | `maintain-belief` | simple | LLM-free decay scan (escalation-exempt) |
| `consolidatable_memory` | `consolidate-memory` | simple | distil memory (escalation-exempt) |
| `contested_claim` | `challenge-belief` | **swarm** | K skeptics; union/quorum the counter-claims |
| `stale_belief` | `challenge-belief` | **swarm** | K skeptics re-examine |
| `under_evidenced_entity` | `investigate-gap` | **deep** | plan → open investigation → gather → synthesize, across rounds |
| `thin_belief` | `investigate-gap` | **deep** | same |
| `rising_topic` | `investigate-gap` | **deep** | same exploratory |
| `open_investigation` | `dispatch-investigation` | **deep** | multi-round evidence gather until the investigation resolves |
| `contradicted_belief` | `adjudicate-contradiction` | **deep** | plan → gather corroboration → weigh both sides → revise/supersede/reconcile |

**Signal upgrades** (`resolve_tier`): a `merge_candidate` / `redundant_beliefs`
whose similarity lands in the ambiguous band (`> MERGE_LOW`, `< MERGE_HIGH`) is
upgraded simple → swarm. The high-stakes upgrade of a contested belief to deep
adjudication is not done by `resolve_tier` — it is a *separate kind*
(`contradicted_belief`) emitted by its own producer, which also suppresses the
routine `contested_claim` for the same belief.

## Deep agents run across rounds, not in a loop

A deep skill never loops in-process. It emits an *intermediate* effect, the
controller re-senses next round, and the board state tells the skill which step it
is on. The critical invariant: **while a sub-step is in flight, the originating
tension is suppressed**, so the controller doesn't thrash by re-opening the same
work. This reuses the investigation lifecycle (`status`, `opened_by_belief_id`,
`origin`, `collected_claim_ids`) that already encodes that state.

This is the same emergent multi-round flow the gap family has always had
(`investigate-gap` opens an investigation → `dispatch-investigation` gathers →
`extract-source` extracts → `synthesize-belief` reflects it) — now made explicit
and given a flagship case.

## Flagship: contradiction adjudication

When a *fresh* claim contradicts a **load-bearing** held belief (confidence ≥
`MESH_ADJUDICATE_MIN_CONFIDENCE`, supporting-claim fan-in ≥
`MESH_ADJUDICATE_MIN_DEPENDENTS`), the producer emits a deep `contradicted_belief`
tension and the `adjudicate-contradiction` skill (`mesh_agents.skills`) runs:

1. **Plan / gather** — first dispatch (no adjudication investigation yet): open one
   `origin=adjudication` investigation asking "does corroborating evidence support
   or refute this belief?". The normal gather chain works it over the next rounds.
   While it is in flight the `contradicted_belief` tension is suppressed.
2. **Reason / decide** — once that investigation terminates (resolved/abandoned),
   the tension re-surfaces. The skill weighs the belief's own evidence plus the
   gathered corroboration against the fresh contradiction (the shared skeptic core)
   and emits exactly one `ReviseBeliefEffect` (`revised_by_agent="adjudicator"`):
   confidence down on a refutation — dropped from the held set append-only if it
   falls below `MESH_ADJUDICATE_REFUTE_FLOOR` — unchanged on a survival.

The revision **always cites the fresh contradicting claim ids**. That is the
termination guarantee: once cited, the producer's "fresh, unadjudicated" test marks
the contradiction handled and stops re-firing — even when the skeptic is
inconclusive. Adjudication is deliberately revision-only; it never supersedes a
claim, because claims are immutable evidence (a verdict adjusts the belief, not the
record).

## Configuration

The tier knobs are in [`deterministic-controller.md`](deterministic-controller.md)'s
configuration table: `MESH_CONTROLLER_SWARM_SIZE`, `MESH_CONTROLLER_SWARM_QUORUM`,
and the `MESH_ADJUDICATE_*` thresholds.

## Tests

* `tests/test_rules.py` — swarm-tier fans out from the first dispatch; deep tensions
  are not escalated; `contradicted_belief` routes to `adjudicate-contradiction` at
  its priority.
* `tests/test_agenda.py` — per-kind tier defaults, gray-band upgrades, the
  `contradicted_belief` producer + its suppression of the routine challenge and of
  itself while a gather is in flight.
* `tests/test_skill_adjudicate_contradiction.py` — the plan/gather and reason/decide
  steps, in-flight suppression, append-only collapse, the citation termination
  guarantee.
* `tests/test_controller.py` — swarm union vs quorum reconcile, and the deep
  adjudication wired end-to-end through one controller round.
