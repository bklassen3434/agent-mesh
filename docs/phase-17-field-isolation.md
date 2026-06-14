# Phase 17 — Field-Agnostic Core: Isolation, Profiles, Connector Catalog

## Context

Agent Mesh is an A2A-based multi-agent research-tracking system on Postgres
(post-Phase-16). The knowledge engine — claim extraction, semantic entity
resolution, belief synthesis (`score`/`capability`/relational), confidence
scoring, the curator/skeptic/personalizer fleet, procedural heuristics
(Phase 16), the graph, the API, and the wiki — is **already domain-agnostic in
its logic**. What couples the system to "AI + robotics research" is a thin shell:

- **Sources.** arXiv (`cs.AI,cs.RO,cs.LG`) is the only structured ingest; the
  other seven scouts (`hn`, `github`, `bluesky`, `reddit`, `blog`,
  `leaderboard`) default their keywords/topics to AI terms
  (`packages/mesh-agents/src/mesh_agents/*_scout.py`). All eight already emit the
  same neutral `Source` + `ScoutedPaper` shape — a de-facto scout interface that
  is never formalized, never selectable per field.
- **Prompts.** The literal string *"a claim extractor for an AI/robotics research
  knowledge base"* (and AI-only few-shot examples) is hardcoded in
  `CLAIM_EXTRACTION_SYSTEM`, `SKEPTIC_SYSTEM`, and `PERSONALIZER_SYSTEM`
  (`packages/mesh-llm/src/mesh_llm/prompts.py`).
- **No isolation, anywhere.** Nothing in the system has a notion of a field.
  Every field-state table lives in the `knowledge` schema with no scope column:
  the 7 knowledge tables (`entities`, `sources`, `claims`, `beliefs`,
  `belief_revisions`, `relationships`, `investigations`), the procedural store
  (`agent_heuristics` + `agent_heuristic_revisions`, migration 008), the
  operational ledgers (`pipeline_runs`, `llm_usage`, `processed_items`,
  migration 003), and `schedules` (in `public`). Entity resolution blocks by
  `type` only (migration 006) — so "Apple" (tech) and "Apple" (agribusiness)
  would merge. The `processed_items` de-dup ledger is keyed `(source_type,
  external_id)` with no field — so a source relevant to two fields is processed
  once and starved from the second.

**This phase (17)** delivers a working, fully-isolated multi-field system: a
first-class **Field** scopes all field-state, the active **Field Profile** drives
the prompts, and sources come from a formalized **connector catalog** that a
field enables and configures per-field — using the eight existing connectors as
built-ins. The self-serve connector layer (user-addable config-driven connectors)
and the onboarding UX are **Phase 18** (`docs/phase-18-connectors-onboarding.md`),
which depends on this phase.

The existing "AI/robotics" behavior survives as one seeded field, with all prior
data backfilled into it.

Read before writing any code — do not guess table, column, skill, scout, prompt,
or route details:

- The full `knowledge` schema + access layer
  (`packages/mesh-db/migrations_pg/00{2,3,6,8}_*.sql`,
  `packages/mesh-db/src/mesh_db/{entities,claims,beliefs,relationships,investigations,pipeline_runs,episodic}.py`)
- The procedural store + Phase 16 recall/consume path (`agent_heuristics`,
  `recall_history`, the heuristic retrieval query, the consolidation graph)
- The scout family and their shared `Source`/`ScoutedPaper` output; coordinator
  scout discovery (`_DEFAULT_AGENT_URLS`, `_agent_urls`, `discover`),
  `CoordinatorState`, and the run entrypoint (`apps/pipeline/`)
- The three coupled system prompts + `AnthropicClient` `cache_control` prefix
  handling (`packages/mesh-llm/`)
- Semantic entity resolution blocking + merge (`mesh_db.entities`,
  `mesh_agents.entity_resolution`, migration 006)
- The API read dependency + CORS, `make types` (`apps/api/`, `apps/wiki/src/`)
- The `schedules` table + scheduler reconcile loop (`apps/scheduler/`)

---

## Goal

A first-class **Field** (id + stored `FieldProfile`) scopes **everything that is
field knowledge or field-learned state**: entities, sources, claims, beliefs,
relationships, investigations, heuristics, runs, cost, de-dup, and schedules. A
pipeline run is parameterized by a field; the active profile drives the
extractor/skeptic/personalizer prompts and the agents' own scoped memory. Sources
are dispatched from a formalized **connector catalog** + per-field enablement
(built-in connectors only this phase). The seeded `ai-robotics` field reproduces
today's behavior end-to-end.

---

## Principles (do not violate)

- **The engine does not move.** No change to synthesis dispatch, confidence math,
  curator scoring, the claim-type/predicate vocabulary, or the merge
  transaction's semantics. Generalization happens at sources, prompts, and
  scoping only — if a block seems to require engine-logic changes, stop and
  report.
- **Scope everything that is field-state; tag everything that is shared
  runtime.** The boundary is explicit:
  - **Scoped** (carry `field_id`, every read/write filters by it): `entities`,
    `sources`, `claims`, `beliefs`, `belief_revisions` (via belief FK),
    `relationships`, `investigations`, `agent_heuristics`,
    `agent_heuristic_revisions` (via head FK), `pipeline_runs`, `processed_items`
    (PK extended to include `field_id`), `schedules`, and the per-field connector
    config. `llm_usage` inherits field via `run_id` (join), no column needed.
  - **Shared runtime, field-agnostic by design**: the connector *catalog*
    (definitions are reusable across fields; only *enablement + config* is
    per-field), LangGraph checkpoints (`thread_id` is per-run; the run carries
    its field in state), the role/permission model, migrations.
  Default rule for any future table: if it holds field knowledge or
  field-learned behavior, it gets `field_id`.
- **`field_id` is a partition, never a content axis.** It scopes rows; synthesis/
  confidence/curator logic never reads it to branch behavior. Same code, scoped
  data.
- **Resolution and memory never cross fields.** Entity blocking/merge and
  heuristic/episodic retrieval MUST filter by `field_id`. A cross-field merge or
  a heuristic leaking between fields is a correctness bug.
- **Claims immutable; beliefs/heuristics revised append-only.** Unchanged. The
  backfill assigns `field_id`; it never rewrites claim or revision content.
- **Coordinator-owned writes preserved.** `field_id` is set by the coordinator on
  write; no new role gains write. The `fields`/catalog/connector-config tables
  are writer-write, reader-read like the rest.
- **Cache prefix is per-field-stable.** Templating the domain into a system
  prompt moves the `cache_control` prefix per field, but it MUST remain
  byte-stable *within* a field across a run. Build the prefix from the profile
  once per run; never interpolate per-item data into it.
- **Backward compatible by default.** With no field specified, the pipeline
  targets the seeded `ai-robotics` field and behaves exactly as today; the
  `ai-robotics` profile reproduces the current prompts byte-for-byte. No existing
  env var changes meaning.

---

## Scope

### 1. Field model + universal scoping — block 17a

The foundational, hardest-to-retrofit decision. Do the whole cut once.

- **Migration 009** (`packages/mesh-db/migrations_pg/009_fields.sql`):
  - `catalog.fields(id text pk, name text not null, slug text unique not null,
    profile jsonb not null, created_at timestamptz not null default now(),
    is_active boolean not null default true)`. `profile` holds the serialized
    `FieldProfile`.
  - Add `field_id TEXT NOT NULL REFERENCES catalog.fields(id)` to: `entities`,
    `sources`, `claims`, `beliefs`, `relationships`, `investigations`,
    `agent_heuristics`, `pipeline_runs`. (`belief_revisions` and
    `agent_heuristic_revisions` inherit scope through their head FK — no column.)
  - **`processed_items`**: extend the primary key to `(field_id, source_type,
    external_id)` so the same external source can be ingested independently per
    field.
  - **`schedules`** (in `public`): add `field_id` so each field has its own
    cadence; the scheduler reconciles per-field jobs (read the `schedules` shape
    + reconcile loop first).
  - **Backfill:** insert a seeded field `ai-robotics` (name "AI & Robotics",
    profile = today's behavior, see 17b) and set `field_id` to it on every
    existing row across all the above tables in the same migration, then add the
    `NOT NULL` / new PK.
  - Add `field_id` to the composite indexes on the hot filter paths (`claims`,
    `beliefs`, `entities`, `agent_heuristics`), and a partial index for per-field
    resolution blocking (`entities (field_id, type)`).
  - Extend the derived views (migration 004 `belief_signals`, episodic/recall
    views, cost views) to carry/propagate `field_id`. Read the views first.
  - Grants unchanged: `mesh_writer` write on `fields`, `mesh_reader` read.
- **`FieldProfile` + `Field` Pydantic models**
  (`packages/mesh-models/src/mesh_models/field.py`): `slug`, `name`,
  `description` (grounding sentence(s) for prompts), `entity_type_hints`,
  `extraction_examples` (few-shot), `topic_label`. **Connector enablement lives
  in 17c**, not the profile. Validate round-trip through `jsonb`.
- **`mesh_db.fields`** access module: `get_field`, `list_fields`, `create_field`,
  `set_active`. Reader-safe reads; writer-only writes.
- **Thread the active field through a run.** Add `field_id` to `CoordinatorState`
  and to the run entrypoint (`--field <slug>`, default `ai-robotics`). Every
  coordinator write stamps the run's `field_id`; every read it does for
  synthesis/resolution/memory scopes to it.
- **Scope entity resolution.** `find_candidate_duplicates` /
  `resolve_entity_semantic` / the HNSW query / the merge guard MUST take and
  filter by `field_id`. `reconcile-entities` gains `--field`.
- **Scope memory.** Heuristic retrieval (Phase 16d query) and `recall_history`
  filter by `field_id` (via the heuristic column and via `pipeline_runs.field_id`
  + claim provenance respectively). The consolidation graph writes heuristics
  scoped to the field whose history it distilled.
- **Scope the read API.** Every `/api/v1/*` knowledge endpoint takes a `field`
  query param (default `ai-robotics`) and filters by it, incl. `/graph/data` and
  the cost/run endpoints. Regenerate `make types`.

**Exit:** migration applies cleanly and backfills all existing rows (knowledge +
heuristics + runs + processed_items + schedules) into `ai-robotics`; a run with
no `--field` reproduces today's behavior end-to-end; resolution provably never
merges across fields and heuristics provably never leak across fields (tests with
a shared name / shared agent+skill in two fields); `/api/v1/*` filters by field;
`ruff` + `mypy --strict` clean; existing pytest + Playwright unaffected. Tag
`v0.17.0-phase-17a`.

### 2. Profile-driven prompts + scoped memory consumption — block 17b

De-hardcode the three coupled prompts and confirm memory reads are field-scoped.

- Convert `CLAIM_EXTRACTION_SYSTEM`, `SKEPTIC_SYSTEM`, `PERSONALIZER_SYSTEM` into
  **builders** taking a `FieldProfile`: domain line from `profile.description`;
  few-shot from `profile.extraction_examples`; entity-naming guidance from
  `profile.entity_type_hints`; `sota:`/topic wording from `profile.topic_label`.
  The **predicate vocabulary and object schemas stay identical** — only framing
  and examples are field-supplied.
- The `ai-robotics` seeded profile carries the *current* description + existing AI
  few-shot verbatim, so its built prompt is **byte-identical** to today's
  hardcoded string — assert this (pins the cache prefix, proves zero behavior
  change).
- Plumb the profile to each agent (coordinator passes `field_id`; agent fetches +
  caches the profile per run; builds the `cache_control`-marked prefix once).
  Per-item content (and the Phase 16 episodic/heuristic blocks, already after the
  prefix) stays after it.
- Confirm the 17a memory scoping holds end-to-end: an agent running in field B
  sees only field-B heuristics + history in its prompt.

**Exit:** all three prompts build from the profile; `ai-robotics` built prompt ==
prior hardcoded string (asserted); a second field yields a reframed prompt;
memory blocks are field-scoped; cache prefix stable within a field; `ruff` +
`mypy --strict` clean. Tag `v0.17.0-phase-17b`.

### 3. Connector framework — catalog + per-field enablement — block 17c

Formalize the de-facto scout interface and make sources a *catalog* configured
per field. Built-in connectors only this phase; user-addable connectors are
Phase 18.

- **`SourceConnector` protocol** in `mesh-agents` capturing what all eight scouts
  already do: `scout(field_profile, config, max_results, since) -> list[Source +
  payload]` plus optional `investigate(...)`. Refactor the existing scouts to
  declare conformance (no behavior change) and read their categories/keywords/
  topics from **per-field connector config** instead of module constants.
- **Connector catalog** (`catalog.connectors`, seeded in the migration): each
  row is a connector *definition* — `id/slug, name, description, kind`
  (`builtin`), and a JSON `config_schema` describing the fields a user must supply
  (e.g. arXiv → `categories`; github → `topics`). Seed the eight existing
  connectors. Catalog is global (reusable across fields), reader-readable.
- **Per-field enablement** (`catalog.field_connectors`: `field_id,
  connector_id, config jsonb, enabled bool, unique(field_id, connector_id)`,
  coordinator-write). The coordinator discovers/dispatches only the connectors
  enabled for the run's field, passing each its stored `config`.
  `_DEFAULT_AGENT_URLS` becomes the set of *available* connector services;
  *which* run is field-driven.
- Validate a connector's `config` against its `config_schema` on enable (reject
  bad config at write time, not mid-run).
- The seeded `ai-robotics` field enables arXiv+HN+GitHub+… with config equal to
  today's defaults — assert behavior is unchanged.

**Exit:** scouts conform to `SourceConnector` and source search terms from
per-field config; the catalog lists the eight built-ins; a field dispatches only
its enabled connectors; the seeded `ai-robotics` field behaves exactly as today
(asserted); `ruff` + `mypy --strict` clean. Tag `v0.17.0-phase-17c`.

### 4. Docs

Add `docs/field-agnostic.md`: the `FieldProfile` shape, the scoping-boundary table
(what's scoped vs shared-runtime), the resolution/memory-never-cross-fields
invariant, the connector model (catalog + per-field config), the per-field
cache-prefix rule. Note Phase 18 extends the connector model with user-addable
connectors + onboarding. Update `CLAUDE.md`'s phase-status paragraph + env-var
table. Match existing `docs/` style (e.g. `docs/entity-resolution.md`).

---

## Out of Scope (this phase — much is Phase 18)

- **User-addable / config-driven connectors (web_search, rss, rest_json) and the
  pubmed reference connector — Phase 18.** This phase formalizes the protocol +
  catalog with built-ins only.
- **Field/connector onboarding UX (CLI `field`/`connectors` groups, wiki field
  switcher + connector picker) — Phase 18.**
- Per-field *engine* tuning (custom confidence weights, predicates, synthesis
  handlers). Vocabulary and math stay universal.
- Cross-field features (federated comparison, cross-field similarity).
- Auth / per-user field ownership / multi-tenant access control. Fields are a
  data scope, not a security boundary.
- Making LangGraph checkpoints field-aware (runs carry field in state).
- Any engine-logic change; any coordinator-write relaxation; any new role;
  mobile; visual redesign.

---

## Exit Criteria

- [ ] Migration 009 applies cleanly; `catalog.fields` present; `field_id NOT
      NULL` on entities/sources/claims/beliefs/relationships/investigations/
      agent_heuristics/pipeline_runs; `processed_items` PK includes `field_id`;
      `schedules` has `field_id`; all pre-existing rows backfilled into
      `ai-robotics`
- [ ] `FieldProfile`/`Field` models added, round-trip through `jsonb`;
      `mypy --strict` clean
- [ ] A run with no `--field` reproduces today's AI/robotics behavior end-to-end
- [ ] Entity resolution provably never merges across fields; heuristics +
      episodic recall provably never leak across fields (shared-name /
      shared-agent+skill tests)
- [ ] The three prompts build from the active profile; `ai-robotics` built prompt
      is byte-identical to the prior hardcoded string (asserted); cache prefix
      stable within a field
- [ ] `SourceConnector` protocol formalized; the eight built-ins conform and read
      config from per-field `field_connectors`; coordinator dispatches only
      enabled connectors; `ai-robotics` behavior unchanged (asserted)
- [ ] Connector catalog (`catalog.connectors`) seeded; per-field enablement +
      config validated against `config_schema` on write
- [ ] `/api/v1/*` knowledge/cost/graph endpoints filter by field; `make types`
      clean, no OpenAPI drift
- [ ] `docs/field-agnostic.md` added; `CLAUDE.md` updated
- [ ] `ruff` + `mypy --strict` clean across touched packages; existing pytest +
      Playwright unaffected
- [ ] Engine logic unchanged; coordinator-owned writes preserved; no role
      relaxation; claims unmodified

---

## Commit Convention

One logical commit per unit; conventional messages:

```
feat(db): add fields table + universal field_id scoping migration + backfill (009)
feat(models): add FieldProfile / Field models
feat(db,agents): scope entity resolution + memory retrieval by field_id
feat(coordinator): thread active field through the run + scoped writes
feat(api): filter knowledge/cost/graph endpoints by field
feat(llm): build extractor/skeptic/personalizer prompts from FieldProfile
feat(db,agents): add SourceConnector protocol + connector catalog + per-field config
docs: add field-agnostic.md; update CLAUDE.md
```

Tags map to blocks: `v0.17.0-phase-17a` (field model + universal scoping), `…-17b`
(profile-driven prompts + scoped memory), `…-17c` (connector framework: catalog +
per-field config). Execute 17a → 17c in order — 17a (the scope cut) is a hard
prerequisite for everything. Lint, types, and a clean back-compat run are the bar
before each tag. Report any principle conflict (e.g. a view can't carry
`field_id` without an engine-logic change) before working around it.

---

## Done = a working multi-field system

At the end of Phase 17 the system hosts multiple fully-isolated fields, each with
its own profile-driven prompts, scoped knowledge + memory, and a configured set of
built-in connectors. **Phase 18** (`docs/phase-18-connectors-onboarding.md`) adds
the self-serve layer: user-addable config-driven connectors and the field/connector
onboarding UX.
