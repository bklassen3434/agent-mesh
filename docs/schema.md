# Schema Reference

## Storage: Postgres only

The knowledge store is a single Postgres instance (the `mesh-postgres` container, `pgvector/pgvector:pg16`). DuckDB has been fully removed (Phase 12). Two schemas live in this one database:

- **`knowledge`** — all the domain + operational knowledge tables described below. Connections set `search_path TO knowledge, public`, so queries reference these tables/views unqualified.
- **`public`** — the operational tables: the **LangGraph checkpoints** (one thread per run, `thread_id == run_id`) and the **`schedules`** table (interval/enabled config the scheduler reconciles). The old `agent_tasks` / `agent_task_events` durability tables were **dropped** in Phase 8 — `/status` now reads orchestration state from the checkpoint store.

Conventions:
- Arrays are Postgres `text[]` (`unnest(arr)` / `cardinality(arr)` / `x = ANY(arr)`).
- Vector columns are pgvector `vector(384)` with **HNSW cosine** indexes (`vector_cosine_ops`, `<=>`). Embeddings come from `fastembed` / `BAAI/bge-small-en-v1.5` (cosine-normalised, 384-dim).
- JSON is `JSONB`; floats are `DOUBLE PRECISION`.
- Schema + roles are applied via numbered SQL in `packages/mesh-db/migrations_pg/NNN_*.sql`, run by `mesh_db.pg_migrations.init_pg()` (idempotent, tracked in `knowledge.migrations`).

See `docs/postgres-migration.md` for the migration rationale and `docs/field-agnostic.md`, `docs/entity-resolution.md`, `docs/belief-synthesis.md`, `docs/belief-consolidation.md`, `docs/agent-observability.md` for the per-feature detail behind the structures below.

## Roles enforce write-ownership

The coordinator-owned-write model is enforced at the DB level by two roles (`mesh_db.pg_migrations.ensure_roles`):

- **`mesh_writer`** — coordinator / skeptic-sweep / CLI / migrations. `SELECT/INSERT/UPDATE` on the `knowledge` schema, but **no DELETE/TRUNCATE** by default — this backs the claim-immutability and revision-append-only invariants in the database itself. The only DELETE grants are narrow: **migration 006 grants DELETE on `entities` and `relationships` only** (entity merge re-points then removes the absorbed duplicate). Belief consolidation deliberately adds **no DELETE grant** (a merged-away belief is marked not-held, never deleted).
- **`mesh_reader`** — `apps/api`. `SELECT` only. The read-only API posture is enforced by grants, not just convention.

## Core principle: claims are immutable, beliefs are mutable

A **claim** is a historical record of what a source asserted at a point in time. Once inserted, its content fields (predicate, subject, object, source, raw_excerpt) never change. This is non-negotiable: modifying a claim would silently destroy provenance. If new evidence supersedes a claim, you create a new claim and mark the old one `superseded`. The only allowed update path is `update_claim_status()`; no general-purpose `update_claim()` exists.

A **belief** is the system's current synthesized view on a topic. It is explicitly mutable and carries a `revision_count`. Every revision is recorded in `belief_revisions` (append-only — never updated or deleted), so the full audit trail is always available. Belief consolidation (merge / decay / archive) only ever flips `is_currently_held` or recomputes confidence; it never deletes a belief row.

## Provenance is mandatory

- Every `Claim` must reference a `Source`. No orphan claims.
- Every `Belief` carries lists of supporting and contradicting `Claim` IDs.
- Every `Relationship` carries `evidence_claim_ids`.
- `Investigation.resolution_belief_id` closes the loop from question to answer.

## Field scoping (Phase 17)

The core is field-agnostic. A first-class **Field** (`knowledge.fields`) scopes all field-state. A `field_id` FK is present on `entities`, `sources`, `claims`, `beliefs`, `relationships`, `investigations`, `agent_heuristic`, `pipeline_runs`, `processed_items`, and `agent_invocations` (and on `schedules` in `public`). Revisions inherit scope through their head FK (`belief_id` / `heuristic_id`) and get no column; `llm_usage` inherits via `run_id`. `field_id` is a **partition, never a content axis** — synthesis/confidence/curator logic never branches on it. The seeded `ai-robotics` field reproduces all prior behavior. Sources are a connector catalog (`connectors`) plus per-field enablement (`field_connectors`).

---

## fields

| Field | Type | Notes |
|-------|------|-------|
| id | TEXT | Primary key (e.g. `ai-robotics`) |
| name | TEXT | Display name |
| slug | TEXT | Unique URL slug; the API's `?field=` key |
| profile | JSONB | Stored `FieldProfile` (drives the prompt builders) |
| created_at | timestamptz | |
| is_active | bool | Fields are deactivated, never deleted (no DELETE grant) |

## connectors / field_connectors

`connectors` is the **global** catalog of source-connector definitions (seeded from `mesh_models.connector.BUILTIN_CONNECTORS` by `init_pg`): `slug`, `name`, `description`, `kind` (`builtin`), and a `config_schema` JSONB. `field_connectors` is one field's enablement + config of a catalog connector (`field_id`, `connector_id`, `config` JSONB, `enabled`, unique on `(field_id, connector_id)`). The coordinator dispatches only the connectors enabled for a run's field, passing each its stored config.

## entities

Represents a named thing in the field's research domain.

| Field | Type | Notes |
|-------|------|-------|
| id | TEXT | Primary key |
| canonical_name | TEXT | The preferred name |
| aliases | text[] | |
| type | EntityType | model, paper, benchmark, method, person, lab, repo, concept |
| attributes | JSONB | Flexible key-value metadata |
| created_at | timestamptz | When first seen |
| last_seen_at | timestamptz | Updated on each re-encounter |
| field_id | FK → fields | |
| name_embedding | vector(384) | **Populated** (Phase 13). HNSW cosine index `idx_entities_name_embedding` backs nearest-neighbour blocking for semantic entity resolution (block → match → merge). See `docs/entity-resolution.md`. |

## sources

Where a claim came from.

| Field | Type | Notes |
|-------|------|-------|
| id | TEXT | |
| type | SourceType | arxiv, hn_post, hn_comment, github, twitter, blog, leaderboard |
| url | TEXT | Canonical URL |
| author | TEXT? | Optional |
| published_at | timestamptz | When the source was published |
| fetched_at | timestamptz | When we retrieved it |
| raw_content_hash | TEXT | SHA-256 of raw content, for dedup |
| reliability_prior | DOUBLE | Bayesian prior on source quality (default 0.5) |
| field_id | FK → fields | |

## claims (IMMUTABLE content)

What a source asserted. Content fields are write-once.

| Field | Type | Notes |
|-------|------|-------|
| id | TEXT | |
| predicate | TEXT | e.g. `achieves_score`, `outperforms`, `developed_by`, `evaluated_on`, plus the Phase-14 predicates `has_capability`, `based_on`, `reproduces`, `critiques`, `speculates` |
| claim_type | TEXT | Derived **1:1 from the predicate** (Phase 14). Routing key the `synthesize` node dispatches on; CHECK-constrained to `score, capability, comparison, attribution, lineage, evaluation, reproduction, critique, speculative`. Unknown predicates fall to the inert `speculative` bucket. |
| subject_entity_id | FK → entities | The thing being described |
| object | JSONB | Flexible value (e.g. `{"value": "175B"}`) |
| source_id | FK → sources | Where this came from |
| extracted_at | timestamptz | |
| extracted_by_agent | TEXT | Which agent created this |
| raw_excerpt | TEXT | Verbatim text that supports the claim |
| status | ClaimStatus | active, superseded, retracted, disputed — MUTABLE (via `update_claim_status` only) |
| confidence | DOUBLE | MUTABLE |
| superseded_by_claim_id | FK → claims? | Points to replacement claim |
| failure_mode | TEXT? | Structured failure mode on Skeptic counter-claims |
| field_id | FK → fields | |

A GIN tsvector index (`idx_claims_fts`, over `raw_excerpt`) backs full-text search (Phase 21).

## beliefs (MUTABLE)

The synthesized current view on a topic.

| Field | Type | Notes |
|-------|------|-------|
| id | TEXT | |
| topic | TEXT | Broad category (e.g. `sota`, `capability:<entity_id>`) |
| statement | TEXT | A human-readable declarative sentence |
| supporting_claim_ids | text[] | Claims that back this belief |
| contradicting_claim_ids | text[] | Claims that challenge it |
| confidence | DOUBLE | Derived from the `belief_signals` view (source diversity, reproduction, skeptic attacks) via `mesh_agents.confidence.compute_confidence` — **no longer hardcoded 0.5** (Phase 14). |
| last_revised_at | timestamptz | |
| revision_count | int | How many times it has been revised |
| is_currently_held | bool | False when retracted, merged-away, or archived |
| statement_embedding | vector(384) | **Populated** on synthesis (Phase 19). HNSW cosine index `idx_beliefs_statement_embedding` backs append-only belief consolidation (block → match → merge). See `docs/belief-consolidation.md`. |
| field_id | FK → fields | |

A GIN tsvector index (`idx_beliefs_fts`, over `topic || statement`) backs full-text search.

## belief_revisions (APPEND-ONLY audit log)

Every time a belief changes, one row is added here. Never updated or deleted (no DELETE grant).

| Field | Type | Notes |
|-------|------|-------|
| id | TEXT | |
| belief_id | FK → beliefs | |
| previous_statement | TEXT | Snapshot before |
| new_statement | TEXT | Snapshot after |
| previous_confidence | DOUBLE | |
| new_confidence | DOUBLE | |
| trigger_claim_ids | text[] | What caused this revision |
| revised_by_agent | TEXT | |
| revised_at | timestamptz | |
| rationale | TEXT | Why the belief changed |

## relationships

Typed, **claim-grounded** edges between entities (Phase 14 turned these into real synthesis output, so `/graph` has edges). `add_relationship_evidence` aggregates duplicate edges.

| Field | Type | Notes |
|-------|------|-------|
| id | TEXT | |
| from_entity_id | FK → entities | |
| to_entity_id | FK → entities | |
| type | TEXT | e.g. `cites`, `trained_on`, `competes_with` |
| evidence_claim_ids | text[] | Claims that support this relationship |
| confidence | DOUBLE | |
| field_id | FK → fields | |

## investigations

Pending questions the system is trying to answer.

| Field | Type | Notes |
|-------|------|-------|
| id | TEXT | |
| question | TEXT | Natural language question |
| related_entity_ids | text[] | |
| status | InvestigationStatus | open, active, resolved, abandoned |
| priority | DOUBLE | |
| created_at | timestamptz | |
| resolved_at | timestamptz? | |
| resolution_belief_id | FK → beliefs? | The belief that answers the question |
| assigned_scout_agents | text[] | |
| target_entity_id | TEXT? | (plain column, no FK) |
| hypothesis | TEXT? | |
| suggested_source_types | text[] | |
| opened_by_belief_id | TEXT? | (plain column, no FK) |
| pipeline_runs_attempted | int | |
| collected_claim_ids | text[] | |
| origin | TEXT | `curator` (default) \| `skeptic` \| `discovery` \| `manual` — who opened it (Phase 22) |
| trigger_rationale | TEXT? | Human-readable "why we opened this" |
| field_id | FK → fields | |

## agent_heuristic / agent_heuristic_revision (procedural memory)

Agents accumulate revisable, provenance-grounded heuristics (Phase 16), modeled on the belief / belief_revision pair: a mutable head row (`agent_heuristic`: `agent`, `skill`, optional `source`/`entity_id`, `heuristic`, `confidence`, `provenance_run_ids`/`provenance_claim_ids`, TTL `expires_at`, `is_currently_active`, `field_id`) plus an append-only revision log (`agent_heuristic_revision`). Same invariants as beliefs: coordinator-owned writes, revisions append-only (no DELETE), provenance mandatory.

## agent_invocations (Phase 23, append-only, field-scoped)

One row per coordinator skill dispatch — the durable answer to "what was this agent thinking?" Writer gets `SELECT/INSERT`, **no DELETE**; reader gets `SELECT`.

| Field | Type | Notes |
|-------|------|-------|
| id | TEXT | |
| run_id | TEXT | Plain indexed column, **not** an FK (`pipeline_runs` is written only at finalize) |
| field_id | FK → fields | |
| agent / skill | TEXT | Who + what was dispatched |
| traceparent / trace_id | TEXT? | W3C trace plumbing; `trace_id` is the Langfuse deep-link key |
| status | TEXT | `ok` \| `error`; `error_type` / `error_message` mirror TaskError |
| input_summary / output_summary | JSONB | Bounded captures (capped by `MESH_OBS_CAPTURE_MAX_CHARS`); raw content stays in Langfuse |
| memory_block | TEXT? | Rendered memory the agent injected (from its optional debug envelope) |
| applied_heuristic_ids | text[]? | |
| system_prefix_hash | TEXT? | |
| model | TEXT? | Realized model |
| latency_ms / input_tokens / output_tokens / cost_usd | numeric | |
| created_at | timestamptz | |

## Operational ledgers (in the `knowledge` schema)

- **pipeline_runs** — per-run counters (`papers_scouted`, `sources_inserted`, `claims_inserted`, …), `errors` JSONB, `run_type`, `triggered_by`, `field_id`.
- **llm_usage** — per-call token/cost ledger (`run_id`, `agent_name`, `skill_id`, `model`, token counts, `estimated_cost_usd`). The realized model is recorded here — routing-tier and consolidation/discovery cost reporting read from it.
- **processed_items** — the dedup ledger; PK extended to **`(field_id, source_type, external_id)`** (Phase 17) so the same external source is ingested independently per field.

## Derived views

Plain views recomputed on read, each carrying `field_id` as a passthrough (Phase 17):

- **belief_reproduction** — distinct-source-type reproduction count per held belief.
- **belief_signals** — source-type diversity, reproduction count, skeptic counter-claim count, severe failure-mode count, 30-day claim velocity. Drives `compute_confidence`.
- **belief_hype_substance** — a bounded hype/substance score over `belief_signals`.

---

## Example flow: a new arxiv paper appears

1. **Source created**: a row is inserted into `sources` with `type=arxiv`, the paper URL, published_at, a content hash, and the run's `field_id`.
2. **Entity resolved/upserted**: before creating any new entity the coordinator runs the semantic guard (alias fast-path → embedding blocking → match) so duplicates merge onto a canonical node; new entities are inserted with a populated `name_embedding`.
3. **Claims extracted**: for each factual assertion, a `claims` row is inserted (immutable from this point), carrying the derived `claim_type`.
4. **Relationships recorded**: relational claim types (comparison/attribution/lineage/evaluation) become claim-grounded `relationships` edges; duplicate edges aggregate evidence.
5. **Beliefs synthesized**: the `synthesize` node dispatches on `claim_type`; new/changed beliefs get a `statement_embedding` and a confidence derived from `belief_signals`, and every change appends a `belief_revisions` row.
6. **Investigations resolved**: an open investigation answered by this run is set `resolved` with `resolution_belief_id` pointed at the belief.
7. **Capture**: each skill dispatch records an `agent_invocations` row (best-effort, written at finalize behind the run-exists guard).
