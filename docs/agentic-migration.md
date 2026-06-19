# Agentic migration — from scheduled pipeline to a self-directed market

This is the shared map for migrating Agent Mesh from a fixed, scheduled assembly
line to an agentic, **blackboard/market** system: the knowledge store is a board,
the things that need attention are derived as **tensions**, a **market** funds the
most valuable ones under a budget, and **skills** (the unit of capability) do the
work — emitting **effects** that a single **write gateway** applies under the
store's invariants. It runs to **quiescence** and wakes on new evidence.

> Plain-English version: instead of a nightly timer pushing papers through fixed
> steps, the system keeps a self-writing to-do list, picks the most valuable items
> it can afford, and does them — until there's nothing worth doing.

## Why it's safe to do incrementally

Two rules keep `main` green and let many workspaces run in parallel:

1. **Contract-first.** The shared shapes (`Tension`, `Effect`, `Skill`, `Bid`) are
   frozen on `main` *before* the fan-out. Every worktree branches from a stable
   interface instead of a moving target.
2. **Strangler-fig.** We do **not** rewrite `coordinator.py`. The market grows
   *next to* it as a new entry point. The old pipeline keeps running untouched;
   skills migrate into the market one at a time; when the last one lands, the
   coordinator becomes "the market with one award per round" and is deleted.

## The pieces (and where they live)

| Piece | What it is | Module |
|---|---|---|
| **Board** | the knowledge store (claims/beliefs/entities/…) | Postgres (existing) |
| **Tension** | one item on the to-do list (value + cost) | `mesh_models.tension` ✅ |
| **Agenda** | the ranked tension list + budget clearing | `mesh_agents.agenda` ✅ |
| **Effect** | a typed write *intent* (skills never write) | `mesh_models.effect` ✅ |
| **Write gateway** | applies effects under the invariants | `mesh_db.effects` ✅ |
| **Bid / Skill** | a specialist: bids on a tension, runs it → effects | `mesh_agents.skill` ✅ |
| **Market** | round loop: agenda → bids → clear → dispatch → apply | `apps/pipeline/market.py` (Phase 1c) |

✅ = landed.

## Phases (the dependency DAG)

```
Phase 0  ──→  Phase 1 (contracts)  ──→  Phase 2 (FAN OUT)  ──→  Phase 3  ──→  Phase 4
agenda view   Effect+gateway,           N skills, 1 worktree     control      shadow→
(DONE)        Skill+registry (DONE),     each, parallel          theory       go-live
              Market shell (1c)                                  (osc.)
```

- **Phase 0 — agenda view. DONE.** `mesh.cli agenda` renders the ranked to-do
  list, read-only. De-risks the value function; freezes `Tension`.
- **Phase 1 — contracts.**
  - 1a **Effect + write gateway** — `mesh_models.effect`, `mesh_db.effects`. DONE.
  - 1b **Skill + Bid + registry** — `mesh_agents.skill`. DONE.
  - 1c **Market shell** — `apps/pipeline/market.py` (`mesh-market`). The round
    loop: scan board → skills bid → clear under budget → dispatch → gateway.
    Shadow by default (previews effects, writes nothing); `--apply` to act + loop
    to quiescence. A safe no-op until skills register. DONE.
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
- ✅ Phase 1b — `Skill` / `Bid` / registry.
- ✅ Phase 1c — market shell (`mesh-market`, shadow mode).
- ✅ Phase 2a — tension catalog expanded: `merge_candidate`, `contested_claim`,
  `unsynthesized_claims` now appear on the agenda with handler skills assigned.
- ⏭️ Phase 2b — skill fan-out (spin up the worktrees above). **Next — parallel.**

The skeleton is complete and runnable end-to-end (`mesh-market` shadow → live).
Every tension kind the skills target now appears on the agenda; the fan-out is
additive, low-conflict, parallel work.
