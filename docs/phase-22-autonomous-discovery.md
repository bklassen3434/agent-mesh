# Phase 22 — Autonomous Discovery: Self-Directed Investigation From Gaps & Trends

## Context

Agent Mesh ingests on a fixed cadence: the scheduler fires `mesh-pipeline`, the
enabled connectors fetch whatever their config says to fetch, and claims/beliefs
accumulate. There is already a *reactive* seed of agency — the **Curator**
(`mesh_agents/curator.py`) inspects held beliefs and, per its
`_should_investigate` heuristic (thin evidence, stale evidence, recent
contradicting activity), emits `InvestigationSuggestion`s; the coordinator's
`dispatch_investigations` node then runs `investigate_<source_type>` skills to
go gather targeted evidence, with a lifecycle (`_investigation_lifecycle`) that
resolves or abandons each Investigation.

But this machinery is **half-built and purely belief-local**:

- Most `investigate_*` handlers are **stubs** (`make_empty_investigate_handler`);
  only arxiv (and partly github/leaderboard) actually search. So even when the
  Curator opens an investigation, little real fetching happens.
- The Curator only ever looks **one belief at a time**. Nothing looks at the
  *field as a whole* and asks "given everything we know and what's trending, what
  should we go find out next?" — no notion of a coverage gap, an under-evidenced
  entity, or a rising topic the mesh is under-sampling.

This phase makes the system **proactively agentic**: a scheduled **discovery
sweep** that analyzes a field's current knowledge state + recent activity to
identify *gaps* and *emerging trends*, drafts concrete investigation hypotheses,
and dispatches **real** hypothesis-directed search (including the universal
`web_search` connector from Phase 18) to close them — all field-scoped,
provenance-stamped, and coordinator-owned. It turns the existing reactive,
belief-local Investigation plumbing into a self-directed loop that expands the
mesh's frontier.

This phase **soft-depends on Phase 18** (the `web_search` connector gives
discovery a universal search tool) and **benefits from Phase 20** (routing keeps
the frequent gap analysis cheap), but it must function against the existing
connectors and without routing — it can start in parallel and adopt both as they
land.

Read before writing any code — do not guess skill, node, table, or column
details:

- The Investigation system end-to-end: `mesh_models.investigation.Investigation`
  (+ `InvestigationStatus`), `mesh_db.investigations` (`create_investigation`,
  `list_investigations`, `update_investigation`, `attach_claim_to_investigation`),
  the coordinator nodes `curate` / `dispatch_investigations` and helpers
  `_open_investigations` / `_investigation_lifecycle` / `_extract_papers`, and
  `apps/pipeline/.../_investigations.py` (`persist_investigation_suggestions`).
- The Curator: `CuratorAgent`, `_should_investigate`,
  `_suggested_source_types_for`, `InvestigationSuggestion` — the rule-based
  precedent for gap detection and source-type routing.
- The `investigate_*` skill pattern: `InvestigateSkillInput` /
  `InvestigateSkillOutput` (`mesh_agents/investigation.py`),
  `investigate_arxiv` (`arxiv_scout.py` `_fetch_papers_by_query`), and
  `make_empty_investigate_handler` (the stubs to replace).
- The connector framework (Phase 17/18): `SourceConnector.investigate`,
  `knowledge.field_connectors` (which connectors a field has enabled + config),
  and — if Phase 18 landed — the `web_search` connector + its `investigate`
  variant.
- Signals to mine for gaps/trends: `belief_signals` / `belief_hype_substance`
  (migration 004), `mesh_db.beliefs.find_stale_beliefs`, `list_beliefs`,
  `mesh_db.entities` (entities with few/zero supporting claims),
  `mesh_db.claims` (claim velocity / recency), the `graph_*` aggregations.
- The scheduled-job template: `apps/pipeline/skeptic_sweep.py` /
  `consolidation.py` (graph shape, `open_checkpointer`, traceparent, batch/sync,
  `pipeline_run_exists` finalize guard, Langfuse cost), `JOB_COMMANDS` +
  `DEFAULT_INTERVALS` (scheduler), the `Makefile` job targets.
- Field scoping (Phase 17): `field_id` everywhere; `load_profile`;
  `mesh_db.fields.list_fields`.

---

## Goal

A scheduled, field-scoped **discovery sweep** (`mesh-discover`) that: (1)
analyzes a field's belief/entity/claim state + recent activity to surface
**knowledge gaps** (under-evidenced entities, thin/stale beliefs, missing
relationships) and **emerging trends** (rising entities/topics the mesh
under-samples); (2) drafts concrete, testable investigation **hypotheses** with
suggested connectors + search terms; (3) opens `Investigation`s stamped with a
`discovery` origin; and (4) dispatches **real** hypothesis-directed search via
upgraded `investigate_*` handlers (incl. `web_search`) to gather evidence —
feeding the existing extract → resolve → synthesize path. Coordinator-owned,
append-only, provenance-stamped, and isolated per field.

---

## Principles (do not violate)

- **Discovery proposes evidence-gathering, never facts.** The sweep opens
  Investigations and fetches sources; it does **not** write claims or beliefs
  directly. New knowledge still flows only through the normal extract → resolve →
  synthesize path on the gathered sources. No shortcut into `beliefs`.
- **Coordinator-owned writes; existing roles only.** Investigation creation +
  source ingestion run under `mesh_writer`, exactly like the pipeline. No agent
  role gains write; no new role.
- **Field isolation is absolute.** Gap/trend analysis, hypothesis drafting, and
  dispatch all scope to one `field_id`. A field's discovery never reads or seeds
  another field. `--field <slug>`, default `ai-robotics`.
- **Bounded and idempotent.** Each sweep opens at most `MESH_DISCOVER_MAX_NEW`
  investigations per field and dispatches at most `MESH_DISCOVER_MAX_FETCH`
  searches; it never re-opens an investigation for a gap an open/recent one
  already covers (dedup against existing `Investigation`s). `log` what was capped.
  Finalize is idempotent via `pipeline_run_exists`.
- **Trusted-input fetches (inherit Phase 18).** Discovery's `web_search` / generic
  fetches are read-only, time-bounded, `max_results`-capped; failures record into
  `state["errors"]` and never abort the sweep — one bad search never kills it.
- **Explainable agency.** Every autonomous Investigation records *why* it was
  opened (the gap/trend rationale + the signals that triggered it) and is stamped
  `origin = "discovery"`, so a human (and Phase 23's observability view) can see
  what the system chose to look into and why. No silent self-direction.
- **Reuse, don't fork, the Investigation plumbing.** Extend the existing
  Curator/`dispatch_investigations`/lifecycle machinery; do not build a parallel
  investigation system. The Curator stays the *reactive* (per-belief) path;
  discovery is the *proactive* (whole-field) path, and both feed the same
  `Investigation` table + dispatch node.

---

## Scope

### 1. Investigation origin + provenance — block 22a

Make autonomous investigations distinguishable and explainable, the smallest
foundational cut.

- Migration `013_investigation_origin.sql` (013 is the next free number after
  Phase 21's `012`; coordinate via the roadmap if numbering shifts): add
  `knowledge.investigations.origin TEXT NOT NULL DEFAULT 'curator'` (values:
  `curator | skeptic | discovery | manual`) + a `field_id, origin` index, and a
  nullable `trigger_rationale TEXT` (the human-readable "why we opened this").
  Backfill existing rows to `'curator'`. Grants unchanged (writer
  insert/update, reader select). No DELETE.
- Thread `origin` + `trigger_rationale` through `Investigation`
  (`mesh_models.investigation`), `create_investigation`, and the existing
  suggestion-persist path so the *current* Curator suggestions carry
  `origin="curator"` with no behavior change.

**Exit:** migration applies + backfills; `Investigation` carries `origin` +
`trigger_rationale`; existing Curator-opened investigations are `origin="curator"`
unchanged; `ruff` + `mypy --strict` clean; existing tests unaffected. Tag
`v0.22.0-phase-22a`.

### 2. Real hypothesis-directed search — block 22b

Replace the stub `investigate_*` handlers with working ones so any opened
investigation actually fetches.

- Implement the `investigate(...)` variant for the connectors a field commonly
  enables, mirroring `investigate_arxiv`'s `_fetch_papers_by_query`: turn the
  investigation's `hypothesis` + `target_entity` into a query, fetch, emit the
  shared `Source`/`ScoutedPaper` shape. Cover at minimum github + leaderboard +
  (if Phase 18 present) `web_search`/`rss`/`rest_json`; keep the remaining stubs
  but make the dispatch tolerant of a connector that has no `investigate`.
- The coordinator `dispatch_investigations` node already wires
  `investigate_<source_type>` → extract → resolve → attach-claim; confirm it
  consumes the now-real handlers, respects the per-field enabled connector set
  (`field_connectors`), and records fetch failures into `state["errors"]`.
- `web_search` is the universal fallback: a discovery investigation in any field
  (even one with no domain connector) can always search the open web for its
  hypothesis. Gate gracefully if Phase 18 hasn't landed (fall back to the
  field's enabled connectors).

**Exit:** an open Investigation with a hypothesis dispatches a *real* search on
its field's enabled connectors (incl. `web_search` when present), gathers
sources, and feeds the extract→synthesize path; failures degrade into
`state["errors"]`; no cross-field fetch; `ruff` + `mypy --strict` clean. Tag
`v0.22.0-phase-22b`.

### 3. Gap + trend analyzer — block 22c

The proactive, whole-field analysis that decides what to look into.

- `packages/mesh-agents/src/mesh_agents/discovery.py`:
  - A **rule-based** `analyze_field(conn, field_id) -> list[GapSignal]` that mines
    the field's state for candidate gaps/trends using existing readers — e.g.
    entities with zero/one supporting claim (under-evidenced), beliefs that are
    `thin`/stale (`find_stale_beliefs`, low `source_type_diversity`), high recent
    claim velocity around an entity/topic (rising trend), comparison beliefs
    missing a reciprocal edge. Each `GapSignal` carries the triggering signals +
    a machine rationale. No LLM here — mirror the Curator's rule-based posture.
  - An **LLM** `draft_hypotheses(profile, gap_signals) -> list[DiscoveryProposal]`
    that turns the top gap signals into concrete, testable investigation
    hypotheses with `suggested_source_types` + search terms, framed by the
    `FieldProfile` (built like the Phase-17 prompt builders). Conservative: it
    proposes *what to search for*, never asserts answers. Model via
    `make_routed_llm_client(agent_name="discovery")` (cheap tier; falls back to
    `make_llm_client`). Tolerates `LLMResponseError` (skip a bad proposal, never
    crash the sweep).
  - Each `DiscoveryProposal` maps to a `create_investigation(... origin="discovery",
    trigger_rationale=...)`, deduped against already-open/recent investigations
    for the same gap.

**Exit:** `analyze_field` surfaces under-evidenced entities + thin/stale beliefs +
rising-activity topics as `GapSignal`s from existing data; `draft_hypotheses`
turns them into testable, field-framed proposals and degrades on LLM failure;
unit-tested with a seeded corpus + mock `LLMClient`; `ruff` + `mypy --strict`
clean. Tag `v0.22.0-phase-22c`.

### 4. Discovery sweep job + schedule + CLI — block 22d

Wire it as a scheduled job, mirroring the existing sweeps.

- `apps/pipeline/src/mesh_pipeline/discovery.py` — a LangGraph job cloned from
  `skeptic_sweep.py` (`open_checkpointer`, traceparent, `pipeline_run_exists`
  finalize guard, Langfuse cost). Per active field: `analyze_field` →
  `draft_hypotheses` → open `discovery` investigations (capped by
  `MESH_DISCOVER_MAX_NEW`) → dispatch real hypothesis-directed search (capped by
  `MESH_DISCOVER_MAX_FETCH`) reusing the coordinator's investigate→extract→
  synthesize path → finalize. De-dup against existing open investigations.
- Entry point `mesh-discover = mesh_pipeline.discovery:main`
  (`apps/pipeline/pyproject.toml`). Scheduler: add `discovery` to
  `DEFAULT_INTERVALS` (default **24h**) and
  `JOB_COMMANDS["discovery"] = ["uv","run","mesh-discover"]`. `Makefile`: a
  `discover` target mirroring `consolidate`. No new container.
- CLI: `mesh.cli discover [--field <slug>] [--apply] [--report-path …]` — dry-run
  by default (lists the gaps + drafted hypotheses it *would* open), `--apply` to
  actually open investigations + dispatch; plus `mesh.cli investigations list`
  gains an `--origin` filter so discovery-opened investigations are inspectable
  (mirror existing list filters).

**Exit:** `make discover` runs end-to-end per active field; opens ≥1
`discovery`-origin investigation on a seeded corpus with gaps (or cleanly no-ops
on a saturated one), dispatches real search, and feeds the synthesize path;
caps respected + logged; idempotent finalize; cost attributed in Langfuse;
`mesh.cli discover` dry-run report works; `ruff` + `mypy --strict` clean. Tag
`v0.22.0-phase-22d`.

### 5. Docs — block 22e

Extend `docs/investigations.md` (or add `docs/autonomous-discovery.md` and
cross-link) with: the reactive-Curator vs proactive-discovery split, the gap/trend
signal taxonomy, the propose-evidence-never-facts contract, the `origin`
provenance, the caps + idempotency, and field isolation. Match existing `docs/`
style. Update `CLAUDE.md`'s phase-status paragraph + env-var table (`MESH_DISCOVER_*`
knobs, `MESH_LLM_MODEL_DISCOVERY`).

---

## Out of Scope (do not build)

- **Writing claims/beliefs directly from discovery.** New knowledge flows only
  through extract → resolve → synthesize on gathered sources. No shortcut.
- **Auto-enabling new connectors or auto-editing connector config.** Discovery
  may *suggest* a new RSS feed / search query in its report, but enabling
  connectors stays the human/Phase-18 onboarding path — no autonomous config
  writes this phase.
- **Open-ended web agents / tool-use loops / autonomous browsing beyond the
  connector `investigate` contract.** Discovery dispatches bounded, capped
  searches through existing connectors, not a free-roaming agent.
- **Cross-field discovery or transfer.** One field per sweep.
- **A learned/RL gap-prioritization policy.** Rule-based signals + one LLM
  hypothesis-drafting pass only.
- **Replacing or restructuring the Curator.** The reactive per-belief path stays;
  discovery is additive.
- **New wiki UI.** Discovery-opened investigations surface through the existing
  investigations views + Phase 23 observability; no UI work here beyond the
  `--origin` CLI filter.

---

## Exit Criteria

- [ ] Migration `013` adds `investigations.origin` (+ index) + `trigger_rationale`,
      backfills existing rows to `curator`; no role/DELETE change
- [ ] Stub `investigate_*` handlers replaced with real hypothesis-directed search
      for github/leaderboard/`web_search` (+ graceful fallback when a connector
      lacks `investigate`); dispatch respects the field's enabled connectors and
      records failures into `state["errors"]`
- [ ] `discovery.py` agent surfaces gaps/trends from existing signals
      (rule-based) and drafts testable, field-framed hypotheses (LLM, degrades on
      failure); proposals dedupe against open investigations
- [ ] `mesh-discover` job opens capped `discovery`-origin investigations per
      active field and dispatches real search feeding the synthesize path;
      idempotent finalize; cost attributed in Langfuse
- [ ] `mesh.cli discover` (dry-run/apply) + `investigations list --origin` work
- [ ] Discovery writes no claims/beliefs directly; coordinator-owned writes +
      field isolation preserved; caps respected + logged
- [ ] `docs/` updated (discovery section); `CLAUDE.md` phase status + env table
      updated
- [ ] `ruff` + `mypy --strict` clean across touched packages; existing pytest +
      Playwright unaffected

---

## Commit Convention

One logical commit per unit; conventional messages:

```
feat(db,models): add investigations.origin + trigger_rationale (migration 013)
feat(agents): implement real investigate_* hypothesis-directed search handlers
feat(agents): add discovery gap/trend analyzer + hypothesis drafter
feat(pipeline): add discovery sweep job + schedule + make target
feat(cli): add mesh.cli discover + investigations --origin filter
docs: add autonomous-discovery; update CLAUDE.md
```

Tags map to blocks: `v0.22.0-phase-22a` (origin provenance), `…-22b` (real
investigate handlers), `…-22c` (gap/trend analyzer), `…-22d` (sweep + schedule +
CLI), `…-22e` (docs). Execute 22a → 22d in order (22a's `origin` is referenced by
all later blocks); docs last. Lint, types, and a clean field-isolated run are the
bar before each tag. Report any principle conflict (e.g. a dispatch path that
can't stay field-scoped, or a gap signal that would require an engine change)
before working around it.
