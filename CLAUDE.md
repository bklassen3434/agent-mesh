# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Agent Mesh is a persistent multi-agent system for tracking AI/robotics research. The system maintains a living knowledge base built from structured **Claims** (immutable, extracted from sources) synthesized into mutable **Beliefs**.

**Phase status:** Phases 0–7 complete — substrate, end-to-end pipeline, A2A protocol promotion, the read-only FastAPI service (`apps/api`, :8000) + Next.js wiki (`apps/wiki`, :3000), the full scout/skeptic/curator/personalizer agent fleet, the APScheduler service, Investigations activation, and derived belief-quality signals. **Phase 8 complete** — the orchestration layer is now LangGraph: `apps/pipeline/coordinator.py` and `skeptic_sweep.py` are stateful LangGraph graphs (conditional routing + `Send` fan-out) checkpointed to a dedicated Postgres container (`mesh-postgres`), one thread per run (thread_id == run_id). The old `agent_tasks`/`agent_task_events` durability tables were dropped; `/status` reads orchestration state from the checkpoint store. **Phase 9 complete** — wiki UI redesign + schedule control: the nav is now `Daily Brief | Knowledge ▾ | Graph | Pipelines` (shadcn primitives), the knowledge sections live under `/knowledge/*` (old paths redirect), the `/graph` view is a force-directed cytoscape redesign fed by a pre-aggregated `/api/v1/graph/data` endpoint, and a new `/pipelines` page controls scheduling. Schedule config (interval + enabled) lives in a Postgres `schedules` table in the `mesh-postgres` container; the scheduler is now a non-blocking `BackgroundScheduler` with a Starlette HTTP control surface (`/scheduler/status|reload|run/{job_id}`) that reconciles config from Postgres on a 30s poll and on an API reload signal — changes apply without a restart. **Phase 12 complete** — the knowledge store is consolidated onto Postgres. A single Postgres service (`mesh-postgres`, `pgvector/pgvector:pg16`) now holds both the knowledge tables (a `knowledge` schema, with `pgvector` replacing the former `duckdb-vss`) and the operational tables (LangGraph checkpoints + `schedules` in `public`). DuckDB is fully removed. `mesh-db` is a pooled psycopg3 client (`MeshConnection` over `psycopg_pool`); the coordinator-owned-write model is preserved and now also enforced by Postgres roles (`mesh_writer` writes, `mesh_reader` is read-only for the API). Schema + roles are applied via `mesh_db.pg_migrations` (numbered SQL in `packages/mesh-db/migrations_pg/`). See `docs/postgres-migration.md`. **Phase 13 complete** — semantic entity resolution replaces exact-match dedup (block → match → merge). Entities now carry a populated `name_embedding` (pgvector, HNSW cosine; embedder is `fastembed`/`BAAI/bge-small-en-v1.5` via the `Embedder` protocol in `mesh-llm`). `mesh_db.entities` gained `find_candidate_duplicates` (type-filtered blocking), `choose_canonical`, and a transactional `merge_entities` that re-points claim/relationship/investigation references B→A, aggregates colliding edges, folds aliases, and deletes B — never touching claim content (migration 006 also grants `mesh_writer` DELETE on `entities`/`relationships` only). `mesh_agents.entity_resolution` holds the conservative match bands (config-tunable `MESH_ENTITY_MERGE_HIGH`/`_LOW`; middle band → LLM adjudication defaulting to not-same) and the live-path `resolve_entity_semantic` (alias fast-path → block → match). The coordinator runs the semantic guard before creating any new entity; `mesh.cli reconcile-entities` does the one-time backfill cleanup (Batch API). See `docs/entity-resolution.md`. **Phase 14 complete** — belief synthesis is generalized beyond leaderboards. Every claim now carries a `claim_type` (`mesh_models.claim.ClaimType`, derived 1:1 from the predicate; migration 007 + deterministic backfill), and the extractor emits five new predicates (`has_capability`/`based_on`/`reproduces`/`critiques`/`speculates`). The coordinator's `synthesize` node dispatches on `claim_type`: `score` → the unchanged SOTA handler; `capability` → entity-anchored beliefs (`capability:<entity_id>`) that converge per canonical entity (`mesh_agents.synthesis`); relational types (`comparison`/`attribution`/`lineage`/`evaluation`) → claim-grounded edges in the `relationships` table (`add_relationship_evidence` aggregates duplicates), so `/graph` finally has edges. Belief confidence is no longer a hardcoded `0.5` — `mesh_agents.confidence.compute_confidence` derives it from the `belief_signals` view (source diversity, reproduction, skeptic attacks) with config-tunable weights (`MESH_CONFIDENCE_*`). See `docs/belief-synthesis.md`. **Phase 17 complete** — the core is field-agnostic. A first-class **Field** (`knowledge.fields`, `mesh_models.field.Field` + stored `FieldProfile`) scopes all field-state: migration 009 adds a `field_id` FK to `entities`/`sources`/`claims`/`beliefs`/`relationships`/`investigations`/`agent_heuristic`/`pipeline_runs` (revisions inherit via their head FK), extends the `processed_items` PK to `(field_id, source_type, external_id)`, and adds `field_id` to `schedules`; every existing row backfills into the seeded `ai-robotics` field. `field_id` is a partition, never a content axis — synthesis/confidence/curator logic never branches on it. Entity resolution (blocking + name fast-path + reconcile) and memory (heuristic + episodic recall, consolidation) **never cross fields** (`tests/test_field_isolation.py`). The three coupled system prompts are now profile-driven **builders** (`mesh_llm.prompts.build_*` from a `FieldProfile`); the `ai-robotics` profile rebuilds them byte-for-byte (`tests/test_field_prompts.py`), and agents build the `cache_control` prefix once per field via `mesh_agents.profiles.load_profile`. Sources are a formalized **connector catalog** (`SourceConnector` protocol; `knowledge.connectors` seeded from `mesh_models.connector.BUILTIN_CONNECTORS`) + per-field enablement (`knowledge.field_connectors`, validated against `config_schema` on enable); the coordinator dispatches only the run field's enabled connectors, each with its stored config. The read API scopes every knowledge/cost/graph endpoint by `?field=<slug>` (default `ai-robotics`). `run_pipeline`/`mesh-skeptic-sweep`/`reconcile-entities` gain `--field`. The seeded `ai-robotics` field reproduces prior behavior end-to-end. The self-serve connector layer + onboarding UX are Phase 18. See `docs/field-agnostic.md`. **Phase 19 complete** — belief consolidation (the world-model analog of entity resolution, but **strictly append-only**). Beliefs carry a `statement_embedding vector(384)` (pgvector HNSW cosine; migration 011, populated on synthesis from `topic`+`statement` via a local `fastembed` call — no hot-path LLM), and `mesh_db.beliefs` gained the block/choose/merge trio (`find_candidate_duplicate_beliefs` — held-only, field-scoped, family-restricted; `choose_canonical_belief`; transactional `merge_beliefs` that folds claim-id unions, recomputes confidence via an injected `ConfidenceFn`, re-points investigation belief refs, and marks the duplicate `is_currently_held = false`). Migration 011 adds **no DELETE grant** (deliberate contrast with 006) — a merged-away belief is absorbed, not erased, and keeps all revisions. `mesh_agents.belief_consolidation` holds the conservative bands (`MESH_BELIEF_MERGE_HIGH`/`_LOW` `0.95`/`0.85`; middle → LLM adjudication defaulting to not-same) + the write-free `resolve_belief_duplicates`; `mesh_agents.belief_reconcile` holds the shared sweep steps + the synchronous `reconcile_beliefs` the CLI uses. A scheduled LangGraph job (`mesh-belief-consolidate`, `apps/pipeline/belief_consolidation.py`, cloned from the 16c consolidation graph — batch API + sync fallback, finalize-idempotency guard, Langfuse cost) semantically de-duplicates held beliefs then runs a second **LLM-free** pass that decays stale beliefs (confidence half-life) and archives long-dead unsupported ones; every change appends a `BeliefRevision` attributed to `belief_consolidator`, never deleting a row or touching a claim, never crossing fields. Fired daily by the existing scheduler (`belief_consolidation`, 24h) — no new service. `mesh.cli consolidate-beliefs` (one-time backfill + report/`--apply`) and `mesh.cli beliefs duplicates` (read-only candidate-pair view) are both `--field`-aware. See `docs/belief-consolidation.md`. **Phase 20 complete** — tiered model routing. A new `mesh_llm.routing` module adds a `RoutedLLMClient` that implements the same `LLMClient` Protocol and, per request, picks a **cheap tier** by default and **escalates to a strong tier** when a pure, LLM-free difficulty signal fires (`classify_difficulty`: user-content length ≥ `MESH_ROUTE_ESCALATE_CHARS` or an explicit `route_hint`) or the cheap attempt fails to parse (`LLMResponseError` → one retry on strong, `MESH_ROUTE_ESCALATE_ON_PARSE_FAIL`). It is purely additive: `make_llm_client`/`resolve_model` are unchanged, and a new factory `make_routed_llm_client(agent_name=…)` returns a `RoutedLLMClient` only when routing is enabled for the agent (`MESH_ROUTE_ENABLED` / `MESH_ROUTE_<AGENT>_ENABLED`) **and** no static model pin exists (`MESH_LLM_MODEL_*` always wins and is never downgraded). Routing ships **off by default**; with it off behavior is byte-for-byte the prior one. Three call sites opt in (`claim_extractor`, the coordinator's entity-resolution adjudication, the skeptic sync path); every other call site is untouched. Each decision is traced (tier + reason attached to `trace_generation` metadata via a reserved options key the clients strip before the wire) and the realized model is recorded in `llm_usage.model` — **no new table, no migration**. `mesh.cli routing-stats [--field] [--since]` reports the per-tier request/token/cost split from the existing ledger. See `docs/model-routing.md`. **Phase 22 complete** — autonomous discovery turns the reactive, belief-local Investigation machinery into a self-directed loop. **22a**: migration `013_investigation_origin.sql` adds `investigations.origin` (`curator | skeptic | discovery | manual`, default `curator`, `+ (field_id, origin)` index) + a nullable `trigger_rationale` (threaded through `Investigation`/`create_investigation`/`list_investigations`), so who opened an investigation — and why — is always inspectable. **22b**: the stub `investigate_*` handlers are real where it makes sense — `investigate_github` (free-text repo search off the hypothesis), `investigate_leaderboard` (on-demand all-lane snapshot), and `investigate_web` (Brave, the universal fallback) join `investigate_arxiv`; the coordinator's `dispatch_investigations` is refactored to a reusable module-level `dispatch_open_investigations` that now dispatches only to sources backed by a connector **enabled for the run's field** (field isolation extends to the investigation path) and tolerates connectors with no investigate skill. **22c**: `mesh_agents.discovery` is the proactive, whole-field analyzer — rule-based `analyze_field` mines under-evidenced entities, thin/stale beliefs, rising-activity topics, and missing reciprocal edges into ranked `GapSignal`s (two single-query readers added: `entities.under_evidenced_entities`, `claims.recent_claim_counts_by_entity`), and one LLM pass `draft_hypotheses` (field-framed via `build_discovery_system`, `make_routed_llm_client(agent_name="discovery")`) turns them into testable proposals that degrade to `[]` on failure and dedupe (via `build_discovery_investigations`) against open investigations. **22d**: `mesh-discover` (a LangGraph job cloned from the skeptic sweep — `open_checkpointer`, `pipeline_run_exists` finalize guard, Langfuse cost) opens capped (`MESH_DISCOVER_MAX_NEW`) `origin="discovery"` investigations per active field and dispatches real search capped by `MESH_DISCOVER_MAX_FETCH`, reusing `dispatch_open_investigations`; scheduler gains a daily `discovery` job, a `make discover` target, and `mesh.cli discover [--field --apply --report-path]` (dry-run lists the gaps + hypotheses it would open) + `investigations list --origin`. Discovery proposes evidence-gathering, never facts — new knowledge still flows only through extract → resolve → synthesize. See `docs/autonomous-discovery.md`. **Phase 23 complete** — agent observability: inspect what each agent is thinking. A new field-scoped, append-only `knowledge.agent_invocations` table (migration `014`; writer insert/select, reader select, **no DELETE**) records one row per coordinator skill dispatch — bounded input/output summaries, status/error, trace id, latency, model/tokens/cost, and the memory the agent injected (rendered block + applied heuristic ids + system-prefix hash). Capture is coordinator-owned and best-effort: a single `_dispatch` wrapper in `apps/pipeline/coordinator.py` times every `call_skill_node` and builds an `AgentInvocation`; rows accumulate in graph state (an `operator.add` reducer) and write at `finalize` behind the run-exists guard (idempotent, never aborts a run). The shared `dispatch_open_investigations` captures its investigate dispatches the same way. Memory rides an optional, additive **debug envelope** the agent attaches to its skill output (`mesh_agents.memory.debug_envelope`; the `claim_extractor` ships it) — absent → null fields, never blocks. Raw prompts/outputs stay in Langfuse (reached by trace id); only bounded summaries (capped by `MESH_OBS_CAPTURE_MAX_CHARS`) live in Postgres. A read-only `/api/v1/agents*` router (roster, an agent's invocations, one invocation's full detail + Langfuse deep-link, the agent's current memory, the coordinator-star interaction graph) and a wiki **Agents** page (nav is now `Daily Brief | Knowledge ▾ | Graph | Agents | Pipelines`) — click an agent → memory + recent invocations → drill into one invocation's inputs/outputs/context. Extensible by construction: any agent dispatched through the standard skill path appears with no per-agent code (the skeptic sweep's capture is a follow-up). See `docs/agent-observability.md`. **Phase 24 complete** — schema + pipeline rename for delineation. The monolithic `knowledge` schema is split four ways by concern (migration `015_schema_reorg.sql`, `ALTER TABLE … RENAME`/`SET SCHEMA`; object grants survive the move, new schemas get USAGE + default privileges mirroring 005): `knowledge` keeps the domain proper (entities/sources/claims/beliefs/belief_revisions/relationships/investigations + the belief-signal views); `agents` holds memory + observability (`agent_heuristics`, `agent_heuristic_revisions` — both pluralized from their former singular — and `agent_invocations`); `runtime` holds the operational ledgers (`pipeline_runs`, `llm_usage`, `processed_items`); `catalog` holds configuration/reference data (`fields`, `connectors`, `field_connectors`). The pooled connection's `search_path` now spans `knowledge, agents, runtime, catalog, public`, so unqualified queries are unchanged; only schema-qualified literals (owner-connection seed inserts → `catalog.*`) and the two renamed tables touch code. The five pipeline jobs are renamed for clarity (job_id → console script): `pipeline` → `ingest` (`mesh-ingest`), `skeptic_sweep` → `skeptic` (`mesh-skeptic`), `consolidation` → `memory_consolidation` (`mesh-consolidate-memory`), `belief_consolidation` (kept) → `mesh-consolidate-beliefs`, `discovery` (kept) → `mesh-discover`. `pipeline_runs.run_type` values and the `schedules.job_id` rows are remapped to match (the latter via `remap_schedule_job_ids` in `init_pg`, guarded so fresh installs/tests no-op). Make targets follow: `make ingest`, `make consolidate-memory`, `make consolidate-beliefs` (the `mesh-pipeline` Python *package* and the `pipeline_runs` table name are unchanged). **Agentic market — full ingest-loop coverage** — the market (`apps/pipeline/market.py`, `mesh-market`) now does everything the coordinator's ingest loop does, under one budget: **scout → extract → resolve/merge → synthesize → challenge → investigate (open + dispatch)**. Source acquisition is in-process (`mesh_agents.connector_dispatch` calls the same `_handle_scout_<slug>`/`_handle_investigate_<name>` handlers the A2A scout servers wrap — no fleet to run); `scout-source` (tension `unscouted_connector`) and `dispatch-investigation` (tension `open_investigation`) are registered skills alongside the original five. New effects: `CreateEntityEffect` (so `extract-source` mints unseen subjects — embedded for merge-candidate blocking — and a fresh field bootstraps), `UpdateInvestigationEffect`/`AttachClaimToInvestigationEffect` (so opened investigations gather evidence, attach claims, and resolve/abandon on the `MESH_INVESTIGATION_*` thresholds). The write gateway (`mesh_db.effects.apply_effects`) gained an injected `confidence_fn` so market-synthesized beliefs get the same `belief_signals`-derived confidence the coordinator computes (Phase 14d). Migration `016` adds a nullable `sources.payload` so the scouted title/abstract survives the round between scout and extract. The market loop keeps a per-run `dispatched` set (in-run oscillation guard → reaches quiescence) and writes a `pipeline_runs` row (run_type `market`) per live run, so runs show up in `/status`, the Pipelines page, and `pipeline-stats`. `market` is a scheduler job (`mesh-market --apply`, `make market`/`market-apply`) seeded **disabled** — flip it on per field from the Pipelines page once shadow output looks right, so it never double-writes alongside the coordinator (strangler-fig go-live). Remaining gaps before retiring the coordinator: per-skill `llm_usage`/`agent_invocations` capture (needs the `Skill.run` contract to surface usage) and inline belief `statement_embedding` on market synthesis (currently left to the consolidation backfill). See `docs/agentic-status.md`.

## Commands

```bash
# Setup
uv sync
cp .env.example .env
uv run mesh.cli init-db        # applies the Postgres schemas (knowledge/agents/runtime/catalog) + roles (idempotent)

# Run the pipeline (defaults to Anthropic Claude Haiku 4.5; needs ANTHROPIC_API_KEY in .env. Switch to local Ollama with MESH_LLM_PROVIDER=ollama)
uv run mesh-ingest
uv run mesh-ingest --categories cs.LG --max-papers 50 --since 7d

# Inspect pipeline output
uv run mesh.cli pipeline-stats
uv run mesh.cli show-sota-beliefs
uv run mesh.cli show-recent-claims
uv run mesh.cli ollama-check

# Tests (no LLM needed — uses mocked clients)
uv run pytest
uv run pytest tests/test_orchestrator.py   # single file

# Lint / type check
uv run ruff check .
uv run ruff check . --fix
uv run mypy .

# Phase 3: read API + wiki
uv run mesh-api                            # FastAPI on :8000; /docs for Swagger
make wiki                                  # opens http://localhost:3000
make api                                   # opens http://localhost:8000/docs
make types                                 # regenerate apps/wiki/src/lib/api-types.ts
cd apps/wiki && npm run dev                # wiki dev mode against a running API
cd apps/wiki && npm run build              # production build (used by Dockerfile.wiki)
```

CI runs `ruff check`, `mypy`, and `pytest -v` on every push.

## Architecture

This is a `uv` workspace monorepo. Dependency flow is strictly one-way:

```
mesh-models  ←  mesh-db  ←  mesh-agents  ←  apps/pipeline
mesh-tracing  ←  mesh-llm  ←  mesh-agents
apps/cli    (depends on mesh-db, mesh-models, mesh-llm)
apps/api    (depends on mesh-db, mesh-models)         # Phase 3
apps/wiki   (TypeScript, Next.js — consumes apps/api) # Phase 3
```

- **`packages/mesh-models`** — Pydantic v2 domain models; no I/O. Seven entities: `Entity`, `Source`, `Claim`, `Belief`, `BeliefRevision`, `Relationship`, `Investigation`.
- **`packages/mesh-db`** — Postgres access layer (pooled psycopg3). One typed module per entity (`entities.py`, `claims.py`, etc.) with a stable public interface; `connection.py` hands out a `MeshConnection` proxy over a `psycopg_pool` pool (`close()` returns to the pool), selecting the writer or reader role by `read_only`. Numbered SQL migrations in `packages/mesh-db/migrations_pg/NNN_*.sql`, applied via `mesh_db.pg_migrations.init_pg()` (idempotent; also creates the `mesh_writer`/`mesh_reader` roles).
- **`packages/mesh-tracing`** — Langfuse wrapper; no-ops when env vars are absent.
- **`packages/mesh-llm`** — Two interchangeable LLM clients implementing the `LLMClient` Protocol: `AnthropicClient` (default; `messages.parse()` for Pydantic-typed structured output with `cache_control` on the system prompt) and `OllamaClient` (local; structured output via `format=schema`). `make_llm_client()` picks one based on `MESH_LLM_PROVIDER`. `LLMResponseError` signals parse failure (pipeline continues); `AnthropicNotReadyError` / `OllamaNotReadyError` signal provider failure (pipeline aborts).
- **`packages/mesh-agents`** — Four agent classes, each with `async run(input) -> output`. `ClaimExtractorAgent` calls the configured LLM via `LLMClient`; `EntityTrackerAgent` does find-or-create against DB; `SotaTrackerAgent` is rule-based (no LLM).
- **`apps/cli`** — Click CLI (`mesh.cli`) wrapping all DB operations with `rich` table output.
- **`apps/pipeline`** — LangGraph orchestration. `coordinator.py` (`run_pipeline`) and `skeptic_sweep.py` (`run_skeptic_sweep`) build `StateGraph`s with conditional edges + `Send` fan-out, checkpointed via `mesh_a2a.checkpoint.open_checkpointer` (Postgres in docker, in-memory locally/tests). Nodes dispatch A2A skills through `mesh_a2a.node.call_skill_node`, which records failures into `state["errors"]` and never raises (one bad paper records an error and continues). Extraction concurrency is still bounded by `asyncio.Semaphore(MESH_PIPELINE_CONCURRENCY)`.
- **`apps/api`** (Phase 3) — FastAPI HTTP service on :8000. One pooled read-only Postgres connection (the `mesh_reader` role) per request via a FastAPI dependency. Endpoints under `/api/v1/`; OpenAPI at `/openapi.json`, Swagger UI at `/docs`. Best-effort idempotent schema-ensure at startup (no-op unless given an owner DSN); all request handling is read-only and enforced by the reader role's grants. **Phase 9** added the only writes: `GET/PATCH /api/v1/schedules` (Postgres `schedules` table), `POST /api/v1/pipelines/{job_id}/trigger` and `GET /api/v1/scheduler/status` (both proxy the scheduler over HTTP via `SCHEDULER_URL`, degrading gracefully when it's down), and `GET /api/v1/graph/data` (pre-aggregated, top-200 nodes by belief count). CORS now allows POST/PATCH from the wiki origin.
- **`apps/scheduler`** (Phase 6a, reworked Phase 9) — non-blocking `BackgroundScheduler` whose jobs shell out to `mesh-ingest` / `mesh-skeptic`. `SchedulerManager` reads interval/enabled config from the Postgres `schedules` table (via `mesh_a2a.schedules`), tracks per-job running/last-run state, and serves a Starlette HTTP control surface (`/scheduler/status`, `/scheduler/reload`, `/scheduler/run/{job_id}`) on :9100. `reconcile()` re-applies config to live jobs without a restart — on a 30s poll and on the API's reload signal. `configured_cron_triggers` is retained only for the legacy `/status` page.
- **`apps/wiki`** (Phase 3, redesigned Phase 9) — Next.js 15 App Router wiki on :3000. Mostly server components; interactive bits (nav dropdown/drawer, Pipelines page, graph) are client components built on Radix-based shadcn primitives in `src/components/ui/`. Nav: `Daily Brief | Knowledge ▾ | Graph | Pipelines`; knowledge sections live under `/knowledge/*` with `next.config` redirects from old paths. TypeScript types live in `apps/wiki/src/lib/api-types.ts`, generated from the API's OpenAPI spec by `openapi-typescript` (`make types`). CI regenerates and diffs to detect drift.

## Key invariants

- **Claims are immutable**: no `update_claim()` exists. Only `update_claim_status()` is allowed. If new evidence supersedes a claim, insert a new claim and mark the old one `superseded`.
- **BeliefRevisions are append-only**: every belief change writes a revision row; never update or delete revision rows.
- **Database connection** is env-driven: `MESH_PG_URL` (owner, used for migrations) or `LANGGRAPH_POSTGRES_URL` as fallback; runtime roles via `MESH_PG_WRITER_URL` / `MESH_PG_READER_URL` (falling back to the base DSN). Tests spin up an ephemeral pgvector container via testcontainers (see `tests/conftest.py`) — never point them at a real DB.
- **`name_embedding vector(384)`** (pgvector) exists on the `entities` table but is intentionally unpopulated (reserved for the entity-resolution phase).
- **Postgres array ops**: arrays are `text[]`; use `unnest(arr)` / `cardinality(arr)` / `x = ANY(arr)`. The connection sets `search_path TO knowledge, public`, so queries reference tables/views unqualified.

## Adding a migration

1. Create `packages/mesh-db/migrations_pg/NNN_description.sql` (Postgres DDL, `knowledge` schema)
2. Run `uv run mesh.cli init-db` — applies only unapplied migrations (tracked in `knowledge.migrations`)

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MESH_PG_URL` | (falls back to `LANGGRAPH_POSTGRES_URL`) | Owner DSN for the knowledge store; used to apply schema + roles |
| `MESH_PG_WRITER_URL` | (falls back to base DSN) | Coordinator/CLI write connection (`mesh_writer` role) |
| `MESH_PG_READER_URL` | (falls back to base DSN) | API read connection (`mesh_reader` role) |
| `MESH_WRITER_PASSWORD` / `MESH_READER_PASSWORD` | `mesh_writer` / `mesh_reader` | Passwords `init_pg` sets on the writer/reader roles |
| `MESH_PG_POOL_MAX` | `10` | Max connections per pool |
| `MESH_LLM_PROVIDER` | `anthropic` | `anthropic` (cloud, Haiku 4.5) or `ollama` (local) |
| `MESH_LLM_MODEL` | `claude-haiku-4-5` | Model ID; matches the provider |
| `ANTHROPIC_API_KEY` | (empty) | Required when provider=anthropic |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL (provider=ollama only) |
| `MESH_PIPELINE_FIELD` | `ai-robotics` | Field slug a pipeline run scopes to (`--field`) |
| `MESH_SKEPTIC_FIELD` | `ai-robotics` | Field slug the skeptic sweep scopes to (`--field`) |
| `MESH_PIPELINE_CATEGORIES` | (unset → field's arxiv connector config) | Optional per-run override of the arxiv connector's categories (`--categories`) |
| `MESH_PIPELINE_MAX_PAPERS` | `20` | Papers per pipeline run |
| `MESH_PIPELINE_CONCURRENCY` | `3` | Parallel LLM slots |
| `LANGFUSE_PUBLIC_KEY` | (empty) | Enables tracing if set |
| `LANGFUSE_SECRET_KEY` | (empty) | Required alongside public key |
| `LANGFUSE_HOST` | `http://localhost:3000` | Langfuse server |
| `API_HOST` | `0.0.0.0` | FastAPI bind host |
| `API_PORT` | `8000` | FastAPI bind port |
| `API_CORS_ORIGINS` | `http://localhost:3000` | Comma-separated CORS allowlist |
| `INTERNAL_API_URL` | `http://api:8000` | Wiki server-component target inside docker |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | Wiki browser target (baked in at build) |
| `LANGGRAPH_POSTGRES_URL` | (empty) | DSN for the single Postgres: LangGraph checkpoints + `schedules` (in `public`) and the `knowledge` schema. Also the base DSN that `MESH_PG_*` fall back to. Unset → in-memory checkpointer + schedule endpoints 503 (local/tests) |
| `LANGGRAPH_POSTGRES_PASSWORD` | `langgraph` | Password for the `mesh-postgres` container |
| `SCHEDULER_URL` | `http://scheduler:9100` | API → scheduler control endpoint (trigger / status / reload) |
| `SCHEDULER_HOST` / `SCHEDULER_PORT` | `0.0.0.0` / `9100` | Scheduler HTTP control bind host/port |
| `NEXT_PUBLIC_LANGFUSE_URL` | (empty) | Optional; when set, the Pipelines run-detail panel links to Langfuse |
| `MESH_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | fastembed model for entity `name_embedding` (384-dim) |
| `MESH_ENTITY_MERGE_HIGH` | `0.93` | Cosine similarity ≥ this auto-merges entities (resolution) |
| `MESH_ENTITY_MERGE_LOW` | `0.80` | Cosine similarity ≤ this auto-rejects; the middle band goes to the LLM |
| `MESH_CONFIDENCE_BASE` | `0.5` | Baseline belief confidence before evidence (Phase 14d) |
| `MESH_CONFIDENCE_SUPPORT_WEIGHT` | `0.5` | Weight on the support term (source diversity + reproduction) |
| `MESH_CONFIDENCE_ATTACK_WEIGHT` | `0.5` | Weight on the attack term (skeptic counter-claims + severe failure modes) |
| `MESH_CONFIDENCE_SOURCE_DIVERSITY_CAP` | `4.0` | Source-type-diversity saturation cap |
| `MESH_CONFIDENCE_REPRODUCTION_CAP` | `3.0` | Reproduction-count saturation cap |
| `MESH_CONFIDENCE_SKEPTIC_CAP` | `4.0` | Skeptic-counter-claim-count saturation cap |
| `MESH_CONFIDENCE_SEVERE_CAP` | `3.0` | Severe-failure-mode-count saturation cap |
| `MESH_ROUTE_ENABLED` | `false` | Global tiered-routing switch (Phase 20) |
| `MESH_ROUTE_<AGENT>_ENABLED` | (inherits global) | Per-agent routing enable; overrides the global flag |
| `MESH_ROUTE_CHEAP_MODEL` | provider default (`claude-haiku-4-5` / `qwen3:8b`) | Cheap-tier model id |
| `MESH_ROUTE_STRONG_MODEL` | `claude-sonnet-4-6` | Strong-tier model id (escalation target) |
| `MESH_LLM_MODEL_<AGENT>_STRONG` | (falls back to `MESH_ROUTE_STRONG_MODEL`) | Per-agent strong-model override |
| `MESH_ROUTE_CHEAP_PROVIDER` / `MESH_ROUTE_STRONG_PROVIDER` | `MESH_LLM_PROVIDER` | Per-tier provider (e.g. cheap local Ollama, strong Anthropic API) |
| `MESH_ROUTE_ESCALATE_CHARS` | `12000` | User-content length (chars) at/above which a request escalates to strong |
| `MESH_ROUTE_ESCALATE_ON_PARSE_FAIL` | `true` | Retry once on the strong tier when the cheap tier fails to parse |
| `MESH_BELIEF_MERGE_HIGH` | `0.95` | Cosine similarity ≥ this auto-merges beliefs (consolidation; tighter than entity resolution) |
| `MESH_BELIEF_MERGE_LOW` | `0.85` | Cosine similarity ≤ this auto-rejects; the middle band goes to the LLM (defaults to not-same) |
| `MESH_BELIEF_CANDIDATE_LIMIT` | `500` | Per-field cap on query beliefs scanned per consolidation run (incrementality bound) |
| `MESH_BELIEF_DECAY_HALFLIFE_DAYS` | `90` | Half-life (days) past which a stale belief's confidence decays |
| `MESH_BELIEF_DECAY_FLOOR` | `0.1` | Minimum confidence a decaying belief floors at |
| `MESH_BELIEF_ARCHIVE_AFTER_DAYS` | `365` | Age (days) past which an unsupported belief is archived (not-held) |
| `MESH_BELIEF_CONSOLIDATION_BATCH` | `true` | Use the Anthropic Batch API for middle-band adjudication (else sync) |
| `MESH_LLM_MODEL_BELIEF_CONSOLIDATOR` | (provider default via `resolve_model`) | Model for belief-merge adjudication |
| `MESH_DISCOVER_MAX_NEW` | `5` | Max `discovery`-origin investigations a sweep opens per field (Phase 22) |
| `MESH_DISCOVER_MAX_FETCH` | `10` | Max source records a discovery sweep gathers per field |
| `MESH_DISCOVER_GAP_LIMIT` | `20` | Max gap signals `analyze_field` returns |
| `MESH_DISCOVER_FIELD` | (unset → all active fields) | `--field` default for `mesh-discover` |
| `MESH_LLM_MODEL_DISCOVERY` | (routing/provider default) | Per-agent model pin for the discovery hypothesis-drafting LLM |
| `MESH_OBS_CAPTURE_MAX_CHARS` | `2000` | Cap on each stored agent-invocation input/output summary; raw content stays in Langfuse (Phase 23) |
| `MESH_MARKET_SCOUT_MAX` | `20` | Per-connector fetch cap for one market `scout-source` poll |
| `MESH_MARKET_INVESTIGATE_MAX` | `10` | Per-investigation fetch cap for one market `dispatch-investigation` run |

## Debugging discipline

- **Check environment before diving into internals.** Weird import errors, `site.py`
  noise, or import machinery failures almost always have an environmental cause:
  project path with spaces, broken editable install, or stale `.venv`/lockfile.
  Look there first.
- **Nuclear reset for inconsistent editable installs.** If some workspace `.pth`
  files load and others don't, skip deeper investigation:
  ```bash
  rm -rf .venv uv.lock && uv sync
  ```
- **Spaces in the project path are a known `uv` editable-install footgun.**
  This repo lives under a path with spaces (`Desktop - Bens MacBook Pro`). If a
  clean rebuild doesn't resolve import issues, the fix is moving the project, not
  more debugging.
- **Verify recovery with both import check and console script, not just one:**
  ```bash
  uv run python -c "import mesh_models, mesh_db, mesh_llm, mesh_agents, mesh_tracing"
  uv run mesh-ingest --help
  ```
## Commit policy
- Create a commit after each logical unit of work (feature, bugfix, refactor step).
- Use Conventional Commits: `feat:`, `fix:`, `refactor:`, `chore:`, `docs:`, `test:`.
- Stage with `git add -A` only after reviewing what changed.
- Do not push to remote unless explicitly asked.
- Skip commits if the working tree is clean.
