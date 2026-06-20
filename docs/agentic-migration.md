# Agentic migration — from scheduled pipeline to a self-directed controller

This is the map for the (now-complete) migration of Agent Mesh from a fixed,
scheduled assembly line to an agentic, **blackboard** system: the knowledge store
is a board, the things that need attention are derived as **tensions**, a
deterministic **controller** picks and prioritises them via an explicit **rule
table**, and **skills** (the unit of capability) do the work — emitting **effects**
that a single **write gateway** applies under the store's invariants. It runs to
**quiescence**.

> **Status:** the migration is complete. The deterministic controller
> (`mesh-controller`) is now the **only** orchestration job — the old scheduled
> LangGraph pipelines (the coordinator/ingest, skeptic sweep, discovery, and the
> standalone consolidation jobs) and their console scripts are deleted; their work
> is now controller rules. See `docs/deterministic-controller.md`.

> **Historical note:** the original design used a *market* — skills bid a value/cost
> on each tension and a budget auction funded the best. That auction was replaced by
> a deterministic rule engine. The blackboard + tensions + skills + effects +
> gateway are all unchanged; only the selection layer (bidding → rules) changed.
> Mentions of "market"/"bid"/"value-per-dollar" below are historical.

> Plain-English version: instead of a nightly timer pushing papers through fixed
> steps, the system keeps a self-writing to-do list, picks the most valuable items
> it can afford, and does them — until there's nothing worth doing.

## Why it's safe to do incrementally

Two rules keep `main` green and let many workspaces run in parallel:

1. **Contract-first.** The shared shapes (`Tension`, `Effect`, `Skill`, `Bid`) are
   frozen on `main` *before* the fan-out. Every worktree branches from a stable
   interface instead of a moving target.
2. **Strangler-fig (now complete).** The controller grew *next to* the old
   `coordinator.py` as a new entry point; skills migrated into it one at a time;
   once the last one landed and the controller was running every step end-to-end,
   the old coordinator/ingest, skeptic-sweep, discovery, and standalone
   consolidation jobs were deleted. The controller is now the only orchestrator.

## The pieces (and where they live)

| Piece | What it is | Module |
|---|---|---|
| **Board** | the knowledge store (claims/beliefs/entities/…) | Postgres (existing) |
| **Tension** | one item on the to-do list (value + cost) | `mesh_models.tension` ✅ |
| **Agenda** | the ranked tension list + budget clearing | `mesh_agents.agenda` ✅ |
| **Effect** | a typed write *intent* (skills never write) | `mesh_models.effect` ✅ |
| **Write gateway** | applies effects under the invariants | `mesh_db.effects` ✅ |
| **Skill** | a specialist: handles a tension kind, runs it → effects | `mesh_agents.skill` ✅ |
| **Rules** | deterministic `state → activations` table (replaced bidding) | `mesh_agents.rules` ✅ |
| **Controller** | round loop: sense → plan(rules) → dispatch → apply | `apps/pipeline/controller.py` ✅ |

✅ = landed. (The former `Bid` type + `apps/pipeline/market.py` are gone — see
`docs/deterministic-controller.md`.)

## Phases (the dependency DAG)

```
Phase 0  ──→  Phase 1 (contracts)  ──→  Phase 2 (FAN OUT)  ──→  Phase 3  ──→  Phase 4
agenda view   Effect+gateway,           N skills, 1 worktree     control      shadow→
(DONE)        Skill+registry (DONE),     each, parallel          theory       go-live
              Controller loop (1c)                              (osc.)
```

- **Phase 0 — agenda view. DONE.** `mesh.cli agenda` renders the ranked to-do
  list, read-only. De-risks the value function; freezes `Tension`.
- **Phase 1 — contracts.**
  - 1a **Effect + write gateway** — `mesh_models.effect`, `mesh_db.effects`. DONE.
  - 1b **Skill + registry** — `mesh_agents.skill`. DONE. (Originally shipped with a
    `Bid` type; the auction was later replaced by the rule engine — `Bid` removed.)
  - 1c **Controller loop** — `apps/pipeline/controller.py` (`mesh-controller`). The
    round loop: sense board → `plan()` over the rule table → dispatch → gateway.
    Shadow by default (previews the plan, writes nothing); `--apply` to act + loop
    to quiescence. (Originally the bidding "market shell"; see
    `docs/deterministic-controller.md`.) DONE.
- **Phase 2 — fan out (parallel).** One worktree per skill (table below).
- **Phase 3 — oscillation control.** Tension identity/idempotency (generalize
  `processed_items`), cooldowns, salience decay, per-target write serialization.
- **Phase 4 — shadow → live.** Flip effects to the gateway behind budget/step
  caps on one field; scheduler heartbeat; wiki agenda page.

## Phase 2 — the worktree map (the parallel part)

Each row is an independent workspace. Each touches **one new file** in
`mesh_agents/skills/` + its own test. The agent logic already exists — the job is
"wrap it to **bid** and **emit effects** instead of writing." Branch from `main`
after Phase 1c lands.

| Worktree | Skill (`skill_id`) | Handles tension kind(s) | Wraps existing |
|---|---|---|---|
| wt-extract | `extract-source` | `unextracted_source` | `claim_extractor` + `entity_tracker` |
| wt-resolve | `merge-candidate` | (new) `merge_candidate` | `entity_resolution` |
| wt-synth | `synthesize-belief` | (post-extract) | `synthesis` / `sota_tracker` |
| wt-skeptic | `challenge-belief` | `stale_belief`, `contested_claim` | `skeptic` |
| wt-discover | `investigate-gap` | `under_evidenced_entity`, `thin_belief`, `rising_topic` | `discovery` + investigations |
| wt-edges | `relationship` | `missing_reciprocal_edge` | relationship synthesis |
| wt-reconcile | `consolidate-*` | (housekeeping) | belief/entity reconcile |

Each skill: a class with `@register_skill`, `skill_id`, `handles`, `bid()`,
`async run()` returning `list[Effect]`. The Phase-0 central scorer in
`agenda.compute_agenda` is the temporary stand-in until each skill owns its `bid`.

### Conflict-avoidance rules for the fan-out

- **One file per skill** under `mesh_agents/skills/`. Never edit another skill's file.
- **Decorator registration only** (`@register_skill`) — no central skill *list* to
  fight over. The single shared edit is appending one `import` line to
  `load_builtin_skills()` in `mesh_agents/skill.py` (append-only, trivial merge).
- **Extending `Effect`** is the one real coordination point. If a skill needs a
  new write kind, add the model in `mesh_models/effect.py` *and* a branch in
  `mesh_db/effects.py`, append-only. Coordinate via the ledger below.

## Migration-number ledger (avoid `NNN_*.sql` collisions)

Parallel worktrees that each add a migration **will** collide on the number.
Rules: Phase 0–2 need **zero** migrations (reads + reuse). Reserve a block here and
claim a number *before* writing the file.

| Migration | Owner / worktree | Purpose | Status |
|---|---|---|---|
| 015 | (landed) | schema reorg | applied |
| **016** | _reserved_ | Phase 3 oscillation state (cooldowns / tension idempotency) | unclaimed |
| 017–019 | _reserved_ | Phase 3/4 overflow | unclaimed |

(Latest applied migration is 015; the next free number is 016.)

## Status

- ✅ Phase 0 — agenda view (`mesh.cli agenda`).
- ✅ Phase 1a — `Effect` + `apply_effects` write gateway.
- ✅ Phase 1b — `Skill` / registry (the `Bid` auction was later removed).
- ✅ Phase 1c — controller loop (`mesh-controller`, shadow mode; was the bidding
  "market shell" — auction replaced by deterministic rules).
- ✅ Phase 2a — tension catalog expanded: `merge_candidate`, `contested_claim`,
  `unsynthesized_claims` now appear on the agenda with handler skills assigned.
- ⏭️ Phase 2b — skill fan-out (spin up the worktrees above). **Next — parallel.**

The skeleton is complete and runnable end-to-end (`mesh-controller` shadow → live).
Every tension kind the skills target now appears on the agenda; the fan-out is
additive, low-conflict, parallel work.
