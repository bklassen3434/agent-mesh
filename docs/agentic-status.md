# Agentic market — skill coverage status

Status of the Phase-2 skill fan-out: every tension kind the board derives now has
a registered skill that bids on it and emits effects. The market runs end-to-end —
`mesh-market` (shadow) → `--apply` (live) — with no "unhandled" tensions.

See `docs/agentic-migration.md` for the architecture. This page is the coverage
ledger: which `TensionKind` maps to which skill, and what (if anything) is left.

## Tension kind → skill

The board derives nine `TensionKind`s (`mesh_models.tension`). All nine are
claimed by one of the five registered built-in skills (`load_builtin_skills()`):

| Tension kind | Skill (`skill_id`) | Wraps | Effects it emits | Status |
|---|---|---|---|---|
| `unextracted_source` | `extract-source` | `ClaimExtractorAgent` + `EntityTrackerAgent` | `CreateClaimEffect` | ✅ handled |
| `merge_candidate` | `merge-candidate` | `entity_resolution` adjudicator | `MergeEntitiesEffect` | ✅ handled |
| `unsynthesized_claims` | `synthesize-belief` | `sota_tracker` + `synthesis` | `CreateBeliefEffect` / `ReviseBeliefEffect` / `AddRelationshipEvidenceEffect` | ✅ handled |
| `contested_claim` | `challenge-belief` | `SkepticAgent` | `CreateSourceEffect` + `CreateClaimEffect` + `ReviseBeliefEffect` | ✅ handled |
| `stale_belief` | `challenge-belief` | `SkepticAgent` | same as above | ✅ handled |
| `under_evidenced_entity` | `investigate-gap` | `discovery` (analyze + draft) | `OpenInvestigationEffect` | ✅ handled |
| `thin_belief` | `investigate-gap` | `discovery` | `OpenInvestigationEffect` | ✅ handled |
| `rising_topic` | `investigate-gap` | `discovery` | `OpenInvestigationEffect` | ✅ handled |
| `missing_reciprocal_edge` | `investigate-gap` | `discovery` | `OpenInvestigationEffect` | ✅ handled |

Five skills, nine kinds, full coverage. The skill→kind mapping is enforced by
`@register_skill` + each skill's `handles` tuple; the agenda's `_KIND_SKILL`
(`mesh_agents.agenda`) names the same handler for every kind, so the
board→skill map and the registry agree.

Every effect kind in the frozen `Effect` union (`mesh_models.effect`) is produced
by some skill and applied by the write gateway (`mesh_db.effects.apply_effects`).
The only union member no skill emits today is `SupersedeClaimEffect` — claim
supersession is still the coordinator's job and has no tension that triggers it
(see below); the gateway branch exists and is exercised by gateway unit tests.

## Nothing is "unhandled"

A tension is *unhandled* only if no registered skill's `handles` contains its
kind — the market counts those as `skipped_no_skill`. With all five skills
registered, that count is **0** for every kind the board can produce.
`tests/test_market_integration.py` asserts this directly: it seeds a small board
(an unread source, a thin belief, a duplicate-looking entity pair), registers the
real skills via `load_builtin_skills()`, runs `run_market(shadow=True)`, and
checks `skipped_no_skill == 0`, `funded == candidates`, and `effects >= 1` — then
a live round materialises a merge + investigations through the gateway.

## Out of scope (deliberately not market skills)

These exist in the system but are *not* expressed as tensions, so the absence of a
market skill for them is by design, not a gap:

- **Housekeeping consolidation** — belief consolidation (Phase 19) and entity
  reconcile (Phase 13) run as their own scheduled LangGraph jobs
  (`mesh-consolidate-beliefs`, `mesh.cli reconcile-entities`). The migration doc's
  `wt-reconcile` "`consolidate-*`" row stays a scheduled job, not a market skill.
- **Relationship synthesis** — the planned standalone `relationship` skill
  (`wt-edges`) was folded into existing skills: `synthesize-belief` emits
  `AddRelationshipEvidenceEffect`s directly from relational claims, and
  `missing_reciprocal_edge` is routed to `investigate-gap` (open an investigation)
  rather than fabricating an edge. No separate skill is needed.
- **Claim supersession** — no `TensionKind` currently derives "this claim is
  superseded," so `SupersedeClaimEffect` has a gateway branch but no skill emitter
  yet. A future tension kind would wire it up.

## Known limitations (pre-Phase-3)

- **No oscillation control / idempotency.** Tension identity and cooldowns are
  Phase 3. In a live multi-round run a skill can re-open the same investigation
  each round until the budget clears it — the integration test pins one live round
  (`max_rounds=1`) to stay deterministic.
- **`extract-source` can't mint entities.** The `Effect` contract has no
  entity-creation effect, so on a fresh field the extractor only emits claims for
  subjects that already resolve to an existing entity; unseen subjects are skipped
  (conservative by design — a new entity is a coordinated effect-kind addition).
- **LLM-bound skills degrade, they don't fail the round.** With no provider
  reachable, `extract-source` and `challenge-belief` return no effects (caught by
  the market's per-skill guard) and `investigate-gap` falls back to a
  deterministic, LLM-free proposal. The market stays a safe no-op rather than
  aborting — the coordinator's "one bad item never fails the run" philosophy.

## Verification

- `TESTCONTAINERS_RYUK_DISABLED=true uv run pytest -q` → **752 passed**
  (includes `tests/test_market_integration.py`, the end-to-end market check).
- `uv run ruff check .` → clean.
- `uv run mypy .` → clean (266 source files).
