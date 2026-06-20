# The deterministic controller — rules, not bidding

The deterministic controller (`mesh-controller`) is the system's **only**
orchestration job: it runs the whole reactive loop (scout → extract → resolve →
consolidate → synthesize → challenge → investigate) in-process. It does not run an
auction. The market metaphor (skills *bid* a value/cost on each tension, the loop
funds the highest value-per-dollar offers under a budget) is gone, replaced by an
**explicit, ordered table of deterministic rules**. The blackboard stays — the
knowledge store is still sensed into a self-writing checklist of `Tension`s every
round — but *what gets done, in what order, by which skill* is now a pure function
of stored numbers, not an emergent price.

> Plain-English version: instead of letting skills haggle over a budget, the
> system has a fixed rulebook ("unread source → extract", "duplicate entities →
> merge", "very similar beliefs → consolidate", "beliefs going stale → decay them",
> "board idle for a while → scout", "a skill keeps failing → throw a swarm at it").
> Same board + same counters + same clock → same plan, every time.

## What changed

| Before (market / auction) | After (deterministic controller) |
|---|---|
| `Skill.bid()` → `Bid(value, est_cost)` | **deleted** — a skill is `skill_id` + `handles` + `run` |
| `score = value / est_cost` ranking | explicit integer **priority** per rule |
| greedy-knapsack budget clearing | per-round **step cap** + per-rule cooldowns |
| `apps/pipeline/market.py` (`mesh-market`) | `apps/pipeline/controller.py` (`mesh-controller`) |
| scheduler job `market` | scheduler job `controller` |

Everything else is unchanged: the same tension producers sense the board, the
same skills do the work, and the same write gateway (`mesh_db.effects`) applies
their effects under the store's invariants.

## The pieces

| Piece | What it is | Module |
|---|---|---|
| **Board** | the knowledge store (claims/beliefs/entities/…) | Postgres |
| **Tension** | one item on the to-do list (board-derived) | `mesh_models.tension` |
| **Sensors** | board → tensions (agenda + scout + investigation producers) | `mesh_agents.agenda` |
| **Counters** | stored per-tension state (attempts/outcome/last-attempt) | `mesh_db.controller_state` |
| **Rule** | a deterministic `state → Activation[]` condition/action | `mesh_agents.rules` |
| **Activation** | run skill *S* on tension *T* at priority *P*, fanout *K* | `mesh_agents.rules` |
| **Controller** | sense → load counters → plan → dispatch → apply → record | `apps/pipeline/controller.py` |
| **Write gateway** | applies effects under the invariants | `mesh_db.effects` |

## The round loop

Each round the controller:

1. **senses** the board into the full candidate tension list (`compute_agenda`
   plus the operational `scout_tensions` / `investigation_tensions` producers);
2. loads the stored per-tension counters (`get_tension_states`) and builds a
   read-only `ControllerState` (tensions + counters + `now`);
3. **plans** — every rule in `RULES` fires; `plan()` keeps the single most-urgent
   `Activation` per tension and returns them sorted `(priority, -salience, id)`;
4. **dispatches** the top `step_cap` activations concurrently → effects;
5. **records** each dispatch's outcome to the counters and applies effects via the
   gateway;
6. repeats until the plan is empty (**quiescence**) or `max_rounds`.

No budget, no prices, no daemon — the only knobs are a per-round `step_cap` and
`max_rounds`. `shadow=True` (the default) previews one round's plan + effects and
writes nothing (no effects, no counters); `--apply` acts and loops to quiescence.

## The rule table

Rules live in `mesh_agents.rules.RULES`, an ordered tuple. Priorities are explicit
integers (lower = more urgent); the planner sorts by them, so an escalation
(priority 0) cleanly pre-empts the normal handler (priority 10+) for the same
tension.

| Rule | Condition | Action | Priority |
|---|---|---|---|
| `escalate-stalled` | a tension was dispatched `≥ N` times and last attempt changed nothing | re-route to the same skill with `fanout = K` (a swarm) | 0 |
| `extract-unread` | an `unextracted_source` tension | `extract-source` | 10 |
| `resolve-duplicate-entities` | a `merge_candidate` tension | `merge-candidate` | 20 |
| `consolidate-redundant-beliefs` | a `redundant_beliefs` tension | `consolidate-beliefs` | 25 |
| `synthesize-claims` | an `unsynthesized_claims` tension | `synthesize-belief` | 30 |
| `dispatch-open-investigations` | an `open_investigation` tension | `dispatch-investigation` | 35 |
| `challenge-contested-beliefs` | a `contested_claim` / `stale_belief` tension | `challenge-belief` | 40 |
| `investigate-knowledge-gaps` | a gap tension (under-evidenced / thin / rising / missing-edge) | `investigate-gap` | 50 |
| `maintain-when-due` | an `aging_belief` / `consolidatable_memory` tension (one per field, maintenance cooldown elapsed) | `maintain-belief` / `consolidate-memory` | 60 |
| `scout-when-idle` | board has no actionable knowledge work **and** scout cooldown elapsed | `scout-source` | 90 |

Skill routing stays a 1:1 map: each tension names its `handler_skill`, so a rule
just forwards it — there was never more than one skill per kind, so nothing needed
an auction to choose.

## Three things make it deterministic and daemon-free

This is the heart of the redesign, and the reason no separate timing process is
needed.

1. **Routing is a map, not a contest.** `Tension.handler_skill` → the skill.

2. **Temporal conditions are state conditions.** "Scout when the field has been
   quiescent for 10 minutes" is not a wall-clock watcher. It is two pure tests:
   the board has no actionable knowledge tension (a board-state query), **and**
   `now - last_scout_at >= cooldown` (arithmetic over a *stored* timestamp and the
   `now` passed into the controller). Whoever invokes the controller — the
   scheduler, a post-run hook, the CLI — gets the same answer, and invoking it
   more often is harmless. There is nothing to "keep running."

3. **Escalation is a counter condition.** "A skill couldn't resolve it — spawn a
   swarm" is "this tension has been dispatched `≥ N` times and the last attempt
   produced no effects (or errored)". The controller then re-routes the tension to
   its *same* skill with `fanout = K`: K instances run in parallel and their
   effects are unioned (deduped). For LLM-bound skills this is a real deep/parallel
   attempt; for rule-based skills the dedup collapses the K identical results to
   one. The stall is read from the stored counters — no timer.

The counters that make (2) and (3) work live in
`runtime.controller_tension_state` (migration 017): one row per `(field, tension)`
with `attempts`, `last_outcome`, `last_effect_count`, `last_attempt_at`. They are
operational state the controller owns directly (writer role), like the
`pipeline_runs` ledger — never routed through the effect gateway.

## Belief consolidation as a rule

The user-facing example "if beliefs are very similar, consolidate them" is a
first-class controller capability:

* a `redundant_beliefs` tension is produced by `find_duplicate_belief_pairs` (a
  pgvector self-join over held, same-family beliefs — the belief analog of the
  entity `merge_candidate` blocking);
* the `consolidate-redundant-beliefs` rule routes it to the **`consolidate-beliefs`**
  skill, which bands the similarity (auto-merge / auto-reject / LLM-adjudicate the
  middle) exactly like `merge-candidate` does for entities;
* a confirmed pair emits a **`MergeBeliefsEffect`**, applied by the gateway via the
  strictly append-only `merge_beliefs` (the duplicate is absorbed and marked
  not-held — no row deleted, no claim touched).

This is the *reactive* path that consolidates as redundancy appears.

## Maintenance as cooldown-gated rules

Belief decay/archival and memory consolidation — formerly standalone scheduled
jobs — are now controller rules, fired on a timer via the same
"temporal-condition = state-condition" pattern as `scout-when-idle`. Both are
**cooldown-gated**: the sensors emit a single tension per field once
`now - last_attempt_at >= MESH_CONTROLLER_MAINTAIN_COOLDOWN_SEC`, and the
`maintain-when-due` rule routes it:

* an `aging_belief` tension → the **`maintain-belief`** skill: an LLM-free decay +
  archival pass that emits append-only `ReviseBeliefEffect`s (the effect gained
  `set_not_held` + `recompute_confidence` flags). It decays stale beliefs toward a
  floor and archives long-dead unsupported ones — never deleting a row or touching
  a claim;
* a `consolidatable_memory` tension → the **`consolidate-memory`** skill: distils
  episodic history into heuristics, emitting a new `WriteHeuristicEffect`. It runs
  synchronously (no Batch-API path).

The standalone `mesh.cli consolidate-beliefs` CLI command (a one-time backfill via
`mesh_agents.belief_reconcile.reconcile_beliefs`) still exists and is unchanged.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `MESH_CONTROLLER_STEP_CAP` | `8` | Max activations dispatched per round |
| `MESH_CONTROLLER_ESCALATE_AFTER` | `3` | Stalled-dispatch count past which a tension escalates to a swarm |
| `MESH_CONTROLLER_SWARM_SIZE` | `3` | Parallel skill instances an escalation fans out to |
| `MESH_CONTROLLER_SCOUT_COOLDOWN_SEC` | `600` | Min seconds between scouts of a connector once the board is idle |
| `MESH_CONTROLLER_MAINTAIN_COOLDOWN_SEC` | `86400` | Min seconds between maintenance passes (belief decay/archival + memory consolidation) per field |

## Running it

```bash
uv run mesh-controller                 # shadow: preview one round's plan, write nothing
uv run mesh-controller --apply         # act + loop to quiescence
uv run mesh-controller --step-cap 4    # smaller per-round batch
make controller / make controller-apply
```

It is the scheduler's sole orchestration job (`controller`, `mesh-controller
--apply`), seeded **enabled** and run per field. It is the only writer of the
ingest loop — there is no separate coordinator to double-write alongside.

## Testing

* `tests/test_rules.py` — the pure rule engine: priority ordering, salience
  tie-breaks, escalation-to-swarm, the temporal-as-state scout rule, determinism.
  No DB, no LLM.
* `tests/test_controller.py` — the loop end-to-end against Postgres (shadow vs
  live, step cap, counter recording, the `controller` pipeline-run row).
* `tests/test_controller_integration.py` — the loop with the real skills
  registered (the production startup path).
* `tests/test_skill_consolidate_beliefs.py` — the new skill + its append-only
  `MergeBeliefsEffect` gateway path.
