# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## How to respond

Straight to the point. No preamble, no padding, no restating the question.

- Lead with the answer. Stop when it's answered.
- Plain words. No jargon unless it's the only word that works — then define it in one line.
- Short sentences. Bullets over paragraphs.
- Don't list every option or trade-off. Give the recommendation.
- When you ask the user something, keep it a simple choice in plain language. No technical detail unless they ask.
- Don't narrate your reasoning or what you're about to do. Just do it, then report briefly.

## What this is

Agent Mesh is a persistent multi-agent system for tracking AI/robotics research. The system maintains a living knowledge base built from structured **Claims** (immutable, extracted from sources) synthesized into mutable **Beliefs**.

**Phase status:** Phases 0–24 complete. The system is live end-to-end. Quick map of what each phase added (see the linked doc for detail):

- **0–7** — Core substrate: the ingest pipeline, A2A agent protocol, the read-only API (`apps/api`, :8000) + Next.js wiki (`apps/wiki`, :3000), the scout/skeptic/curator/personalizer agent fleet, the scheduler, and belief-quality signals.
- **8** — Orchestration moved to LangGraph. `coordinator.py` and `skeptic_sweep.py` are stateful graphs (conditional routing + fan-out) checkpointed to Postgres, one thread per run.
- **9** — Wiki redesign + schedule control. Nav is `Daily Brief | Knowledge ▾ | Graph | Pipelines`; the `/pipelines` page edits a Postgres `schedules` table, and the scheduler reconciles config without a restart.
- **12** — Everything runs on one Postgres (`mesh-postgres`, pgvector). DuckDB removed. Writes go through the `mesh_writer` role; the API reads via `mesh_reader`. See `docs/postgres-migration.md`.
- **13** — Semantic entity resolution (block → match → merge) replaces exact-match dedup, using name embeddings + similarity bands (`MESH_ENTITY_MERGE_*`); the middle band asks the LLM. `merge_entities` re-points references and deletes the duplicate, never touching claims. See `docs/entity-resolution.md`.
- **14** — Belief synthesis generalized beyond leaderboards. Each claim has a `claim_type`; the `synthesize` node handles scores, entity capabilities, and graph edges separately. Belief confidence is computed from evidence signals (`MESH_CONFIDENCE_*`), not hardcoded. See `docs/belief-synthesis.md`.
- **17** — The core is field-agnostic. A **Field** scopes all data via a `field_id` FK; entity resolution and memory never cross fields. Sources are a connector catalog enabled per-field. The API and CLI take `--field` / `?field=`; `ai-robotics` is the seeded default. See `docs/field-agnostic.md`.
- **19** — Belief consolidation: de-dup held beliefs (similarity bands `MESH_BELIEF_MERGE_*`), then decay stale ones and archive dead ones. **Strictly append-only** — a merged belief is marked not-held, never deleted, and keeps its revisions. Runs daily. See `docs/belief-consolidation.md`.
- **20** — Tiered model routing. `RoutedLLMClient` uses a cheap model by default and escalates to a strong one on a hard signal (long input or a parse failure). **Off by default** (`MESH_ROUTE_ENABLED`); a static model pin always wins. See `docs/model-routing.md`.
- **22** — Autonomous discovery. Instead of only reacting to beliefs, the controller's discovery rule (`investigate-gap`) analyzes a whole field for gaps, drafts hypotheses, and opens capped `origin="discovery"` investigations. It proposes evidence-gathering, never facts. (`mesh.cli discover` is a read-only preview.) See `docs/autonomous-discovery.md`.
- **23** — Agent observability. An append-only `agent_invocations` table records one row per skill dispatch (bounded input/output summaries, status, cost, injected memory). A new `/api/v1/agents*` API + wiki **Agents** page let you inspect what each agent did. Raw prompts stay in Langfuse. See `docs/agent-observability.md`.
- **24** — Schema split four ways by concern: `knowledge` (domain), `agents` (memory + observability), `runtime` (ledgers), `catalog` (config). The `search_path` spans all of them, so unqualified queries are unchanged.
- **Controller-only orchestration** — The deterministic **controller** (`mesh-controller`) is now the **sole** orchestration job (seeded enabled; the old fixed `ingest`/`skeptic`/`discovery` jobs and the standalone consolidation sweeps are deleted). It senses the field into `Tension`s and runs the whole loop — scout → extract → resolve → synthesize → challenge → investigate — under an explicit **rule engine** (`mesh_agents.rules`), dispatching up to `MESH_CONTROLLER_STEP_CAP` activations per round (to quiescence) and escalating a stalled tension to a swarm after `MESH_CONTROLLER_ESCALATE_AFTER`. Belief decay/archival and memory consolidation are folded in as **cooldown-gated rules** (`aging_belief`→`maintain-belief`, `consolidatable_memory`→`consolidate-memory`, fired on a timer like scouting; `MESH_CONTROLLER_MAINTAIN_COOLDOWN_SEC`). The LLM **skills are the agentic unit** (prompt + LLM + structured output + injected memory); `ClaimExtractorAgent`/`SkepticAgent` are now thin adapters for the orphaned A2A servers in `apps/agents`. The controller is **self-driving**: `mesh-controller --apply --forever` runs it as an always-on daemon (the default-on `controller` compose service) that repeats the full pass and idles `MESH_CONTROLLER_IDLE_SLEEP_SEC` between empty passes — **no external scheduler/cron**. All cadence comes from the rules' own cooldowns (scouting, maintenance) plus that idle backoff. The old `scheduler` service is legacy (profile-gated, off) — don't run it alongside the daemon. The CLI `discover` (read-only preview) and `consolidate-beliefs` (one-time backfill) remain. See `docs/deterministic-controller.md` (skill coverage in `docs/agentic-status.md`).

## Commands

```bash
# Setup
uv sync
cp .env.example .env
uv run mesh.cli init-db        # applies the Postgres schemas (knowledge/agents/runtime/catalog) + roles (idempotent)

# Run the controller — the sole orchestrator (defaults to Anthropic Claude Haiku 4.5; needs ANTHROPIC_API_KEY in .env. Switch to local Ollama with MESH_LLM_PROVIDER=ollama)
uv run mesh-controller              # shadow: preview one round's plan, write nothing
uv run mesh-controller --apply      # act, looping to quiescence
uv run mesh-controller --apply --field ai-robotics

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
make types                                 # regenerate apps/wiki/src/lib/api-types.ts (self-contained: boots a temp API)
make types-check                           # regenerate + fail if api-types.ts drifted (the CI drift guard)
cd apps/wiki && npm run dev                # wiki dev mode against a running API
cd apps/wiki && npm run build              # production build (used by Dockerfile.wiki)

# Full local CI mirror — run before pushing, ESPECIALLY for API or wiki changes.
make wiki-install                          # one-time: install the wiki's npm deps
make hooks                                 # one-time: activate the pre-push hook (core.hooksPath=.githooks)
make check                                 # ruff + mypy + pytest + types-check + wiki lint/typecheck/build + E2E
```

After `make hooks`, a **pre-push hook** (`.githooks/pre-push`) automatically runs
the relevant guard for what's being pushed — `make types-check` when `apps/api`
changed, `make test-ui` when `apps/wiki` changed — so API↔wiki drift can't reach
CI. Pure Python/docs pushes are untouched. Bypass a push with `git push --no-verify`.

CI runs three jobs: **python** (`ruff check`, `mypy`, `pytest -v`), **wiki**
(lint, typecheck, build, + the api-types drift guard), and **playwright** (wiki
E2E). The Python gate alone does NOT catch API↔wiki contract drift or stale wiki
tests — when you touch `apps/api` handlers/models or a wiki page, run
`make types-check` (for the type contract) and `make test-ui` (for the E2E), or
just `make check` to mirror all of CI locally.

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
- **`apps/pipeline`** — the controller (`controller.py`, `mesh-controller`). A plain async loop, not a LangGraph graph: each round it senses the field into tensions (`mesh_agents.agenda` + scout/investigation/maintenance producers), loads per-tension counters, `plan()`s the worklist via `mesh_agents.rules`, dispatches the top `MESH_CONTROLLER_STEP_CAP` skills concurrently (`asyncio.Semaphore(MESH_PIPELINE_CONCURRENCY)`), records outcomes (incl. one `agent_invocations` row per dispatch + `llm_usage` rows per LLM call), and applies their effects through the `mesh_db.effects` gateway — looping to quiescence (`MESH_CONTROLLER_MAX_ROUNDS`). `--forever` (`run_controller_forever`) wraps that pass in a self-driving daemon (idle backoff between empty passes), so it is its own driver — no scheduler. A bad skill records an error and never aborts the run. (The old `coordinator.py`/`skeptic_sweep.py`/`discovery.py` LangGraph jobs are deleted.)
- **`apps/api`** (Phase 3) — FastAPI HTTP service on :8000. One pooled read-only Postgres connection (the `mesh_reader` role) per request via a FastAPI dependency. Endpoints under `/api/v1/`; OpenAPI at `/openapi.json`, Swagger UI at `/docs`. Best-effort idempotent schema-ensure at startup (no-op unless given an owner DSN); all request handling is read-only and enforced by the reader role's grants. **Phase 9** added the only writes: `GET/PATCH /api/v1/schedules` (Postgres `schedules` table), `POST /api/v1/pipelines/{job_id}/trigger` and `GET /api/v1/scheduler/status` (both proxy the scheduler over HTTP via `SCHEDULER_URL`, degrading gracefully when it's down), and `GET /api/v1/graph/data` (pre-aggregated, top-200 nodes by belief count). CORS now allows POST/PATCH from the wiki origin.
- **`apps/scheduler`** (Phase 6a, reworked Phase 9) — non-blocking `BackgroundScheduler` whose sole job shells out to `mesh-controller --apply` (per field). `SchedulerManager` reads interval/enabled config from the Postgres `schedules` table (via `mesh_a2a.schedules`), tracks per-job running/last-run state, and serves a Starlette HTTP control surface (`/scheduler/status`, `/scheduler/reload`, `/scheduler/run/{job_id}`) on :9100. `reconcile()` re-applies config to live jobs without a restart — on a 30s poll and on the API's reload signal. `configured_cron_triggers` is retained only for the legacy `/status` page.
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
| `MESH_CONFIDENCE_CLAIM_COUNT_CAP` | `8.0` | Supporting-claim-count (evidence-depth) saturation cap — the third support term, so a well-backed belief outranks a one-off even in a single-source-type field |
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
| `MESH_CONTROLLER_MAINTAIN_COOLDOWN_SEC` | `86400` | Min seconds between periodic LLM-free maintenance passes (belief aging, memory consolidation) |
| `MESH_LLM_MODEL_DISCOVERY` | (routing/provider default) | Per-agent model pin for the discovery hypothesis-drafting LLM |
| `MESH_OBS_CAPTURE_MAX_CHARS` | `2000` | Cap on each stored agent-invocation input/output summary; raw content stays in Langfuse (Phase 23) |
| `MESH_ARXIV_DELAY_SECONDS` | `3.0` | Min spacing between arxiv API requests (shared rate-limited client; arxiv 429s under bursts) |
| `MESH_ARXIV_NUM_RETRIES` | `5` | arxiv client retries on a failed page request (429/500) |
| `MESH_MARKET_SCOUT_MAX` | `20` | Per-connector fetch cap for one `scout-source` poll |
| `MESH_MARKET_INVESTIGATE_MAX` | `10` | Per-investigation fetch cap for one `dispatch-investigation` run |
| `MESH_CONTROLLER_STEP_CAP` | `8` | Max activations the controller dispatches per round (replaces the market budget) |
| `MESH_CONTROLLER_MAX_ROUNDS` | `25` | Max sense→plan→dispatch rounds before one run stops short of quiescence (raise so a cold-start ingest drains in one run instead of spilling to the next wake-up) |
| `MESH_CONTROLLER_IDLE_SLEEP_SEC` | `60` | Seconds the self-driving controller (`--forever`) waits after an empty pass before re-sensing. The only timing in continuous mode beyond the rules' own cooldowns |
| `MESH_CONTROLLER_ESCALATE_AFTER` | `3` | Stalled-dispatch count past which a tension escalates to a swarm |
| `MESH_CONTROLLER_SWARM_SIZE` | `3` | Parallel skill instances a swarm-tier dispatch (or an escalation) fans out to |
| `MESH_CONTROLLER_SWARM_QUORUM` | `false` | Swarm reconcile: off = union the K copies' effects; on = keep only effects a majority (`⌈K/2⌉`) agree on |
| `MESH_ADJUDICATE_MIN_CONFIDENCE` | `0.7` | Min belief confidence for a fresh contradiction to be deep-adjudicated (`contradicted_belief`) vs a routine challenge |
| `MESH_ADJUDICATE_MIN_DEPENDENTS` | `2` | Min supporting-claim fan-in before a contradiction is treated as load-bearing |
| `MESH_ADJUDICATE_REFUTE_FLOOR` | `0.2` | Post-adjudication confidence below which a `contradicted` verdict drops the belief from the held set (append-only) |
| `MESH_CONTROLLER_SCOUT_COOLDOWN_SEC` | `600` | Min seconds between scouts of a connector once the board is idle |

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
  uv run mesh-controller --help
  ```
## Commit policy
- Create a commit after each logical unit of work (feature, bugfix, refactor step).
- Use Conventional Commits: `feat:`, `fix:`, `refactor:`, `chore:`, `docs:`, `test:`.
- Stage with `git add -A` only after reviewing what changed.
- Do not push to remote unless explicitly asked.
- Skip commits if the working tree is clean.
