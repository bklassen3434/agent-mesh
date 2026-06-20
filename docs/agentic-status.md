# Agentic market — skill coverage status

Status of the Phase-2 skill fan-out: every tension kind the board derives now has
a registered skill that bids on it and emits effects. The market runs end-to-end —
`mesh-market` (shadow) → `--apply` (live) — with no "unhandled" tensions.

See `docs/agentic-migration.md` for the architecture. This page is the coverage
ledger: which `TensionKind` maps to which skill, and what (if anything) is left.

## Tension kind → skill

There are eleven `TensionKind`s (`mesh_models.tension`). Nine are *board-derived*
(`compute_agenda`); two are *operational* (connector/investigation-config-driven)
and injected by the market loop alongside the agenda (`scout_tensions`,
`investigation_tensions`). All eleven are claimed by one of the seven registered
built-in skills (`load_builtin_skills()`):

| Tension kind | Skill (`skill_id`) | Wraps | Effects it emits | Status |
|---|---|---|---|---|
| `unscouted_connector` | `scout-source` | in-process connector dispatch (scout handlers) | `CreateSourceEffect` | ✅ handled |
| `unextracted_source` | `extract-source` | `ClaimExtractorAgent` + `EntityTrackerAgent` | `CreateEntityEffect` + `CreateClaimEffect` (+ `AttachClaimToInvestigationEffect`) | ✅ handled |
| `merge_candidate` | `merge-candidate` | `entity_resolution` adjudicator | `MergeEntitiesEffect` | ✅ handled |
| `unsynthesized_claims` | `synthesize-belief` | `sota_tracker` + `synthesis` | `CreateBeliefEffect` / `ReviseBeliefEffect` / `AddRelationshipEvidenceEffect` | ✅ handled |
| `contested_claim` | `challenge-belief` | `SkepticAgent` | `CreateSourceEffect` + `CreateClaimEffect` + `ReviseBeliefEffect` | ✅ handled |
| `stale_belief` | `challenge-belief` | `SkepticAgent` | same as above | ✅ handled |
| `under_evidenced_entity` | `investigate-gap` | `discovery` (analyze + draft) | `OpenInvestigationEffect` | ✅ handled |
| `thin_belief` | `investigate-gap` | `discovery` | `OpenInvestigationEffect` | ✅ handled |
| `rising_topic` | `investigate-gap` | `discovery` | `OpenInvestigationEffect` | ✅ handled |
| `missing_reciprocal_edge` | `investigate-gap` | `discovery` | `OpenInvestigationEffect` | ✅ handled |
| `open_investigation` | `dispatch-investigation` | in-process investigate dispatch | `CreateSourceEffect` + `UpdateInvestigationEffect` | ✅ handled |

Seven skills, eleven kinds, full coverage. The skill→kind mapping is enforced by
`@register_skill` + each skill's `handles` tuple; the agenda's `_KIND_SKILL`
(`mesh_agents.agenda`) names the same handler for every kind, so the
board→skill map and the registry agree.

With these the market now runs the coordinator's whole ingest loop end-to-end
under one budget — **scout → extract → resolve/merge → synthesize → challenge →
investigate (open + dispatch)** — acquiring its own sources, minting entities so a
fresh field bootstraps, recomputing evidence-derived belief confidence in the
gateway, and recording a `pipeline_runs` row (run_type `market`) per live run.
Every effect kind in the `Effect` union is produced by some skill and applied by
the write gateway (`mesh_db.effects.apply_effects`); the only member no skill
emits is `SupersedeClaimEffect` (no tension derives "this claim is superseded"
yet — the gateway branch exists and is exercised by unit tests).

## Nothing is "unhandled"

A tension is *unhandled* only if no registered skill's `handles` contains its
kind — the market counts those as `skipped_no_skill`. With all seven skills
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

## Source acquisition + investigation dispatch (market source-of-truth)

The market now acquires its own material rather than assuming sources arrive:

- **`scout-source`** polls each enabled connector **in-process**
  (`mesh_agents.connector_dispatch` calls the same `_handle_scout_<slug>` handlers
  the A2A scout servers wrap — no fleet to run) and emits `CreateSourceEffect`s,
  deduped by content hash. The scouted title/abstract is persisted on the source
  (`sources.payload`, migration 016) so `extract-source` can read the paper text a
  round later.
- **`dispatch-investigation`** works the investigations `investigate-gap` opens:
  it runs the in-process investigate handlers, acquires evidence sources tagged
  with the investigation lineage, and advances the lifecycle (in-progress →
  resolved/abandoned on the same `MESH_INVESTIGATION_*` thresholds the coordinator
  uses). `extract-source` attaches the resulting claims back via
  `AttachClaimToInvestigationEffect`, so investigations actually resolve.

## Go-live (scheduler)

`market` is a scheduler job (`mesh-market --apply`) seeded **disabled** — flip it on
per field from the Pipelines page once shadow output looks right, so it never
double-writes alongside the coordinator (the strangler-fig go-live).

## Known limitations

- **Oscillation control is in-run only.** The market loop keeps a per-run
  `dispatched` set so scouting / investigation tensions don't re-fire each round
  and a run still reaches quiescence; cross-run cooldowns / salience decay remain
  Phase 3.
- **`llm_usage` / `agent_invocations` per skill** aren't captured yet — a market
  run records the `pipeline_runs` ledger row and (when Langfuse is configured) the
  skills' own traces, but per-skill token/cost rows and the Agents-page invocation
  capture still need the `Skill.run` contract to surface usage. Run-level
  observability (`/status`, Pipelines, `pipeline-stats`) works today.
- **Belief `statement_embedding` on market synthesis** is left to the
  consolidation sweep's backfill rather than computed inline by `synthesize-belief`.
- **LLM-bound skills degrade, they don't fail the round.** With no provider
  reachable, the LLM-bound skills return no effects (caught by the market's
  per-skill guard) and the discovery-backed ones fall back to deterministic,
  LLM-free proposals — the coordinator's "one bad item never fails the run"
  philosophy.

## Verification

- `TESTCONTAINERS_RYUK_DISABLED=true uv run pytest -q` → all pass (includes
  `tests/test_market_integration.py` end-to-end, plus `test_skill_scout_source`
  and `test_skill_dispatch_investigation` for the new acquisition paths).
- `uv run ruff check .` → clean.
- `uv run mypy .` → clean.
