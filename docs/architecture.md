# Architecture

## System context

Agent Mesh is a persistent multi-agent system for tracking the state of AI and robotics research. It maintains a living knowledge base built from structured claims extracted from research sources, synthesized into mutable beliefs.

The full system (Phases 1+) will consist of:

- **Scout agents** — crawl arxiv, HN, GitHub, leaderboards, etc. and emit raw source documents
- **Extractor agents** — parse sources and write structured Claims to the database
- **Synthesizer agents** — read new Claims and update Beliefs accordingly
- **Skeptic agents** — challenge low-confidence or contradicted Beliefs
- **Curator agents** — manage entity identity, merge duplicates, maintain quality
- **A2A protocol layer** — agents communicate via a structured agent-to-agent protocol
- **Wiki/API layer** — exposes the living knowledge base to external consumers

## Phase 0 — Foundation (complete)

Phase 0 establishes the substrate. It includes:

- **Repository structure** — uv workspace monorepo with `packages/` and `apps/`
- **Pydantic v2 models** — typed representations of all seven domain entities
- **DuckDB schema** — migrations for all tables; single file database at `./data/mesh.db`
- **Database access layer** — typed read/write functions for each entity; immutability enforced on Claims
- **CLI** — `mesh.cli` with subcommands to create and inspect all entity types
- **Tracing plumbing** — Langfuse wrapper that no-ops without env vars
- **Test suite** — model validation, migration, DB round-trip, and CLI tests
- **CI** — GitHub Actions running ruff, mypy, pytest

## Phase 1 — Local pipeline (current)

Phase 1 wires the first end-to-end loop: arxiv → claims → entities → SOTA beliefs. Everything runs locally; no A2A protocol, no cloud APIs.

New components:

- **`packages/mesh-llm`** — thin Ollama wrapper (`OllamaClient`) with structured output, retry on transient errors, and latency tracking
- **`packages/mesh-agents`** — four agent classes: `ArxivScoutAgent`, `ClaimExtractorAgent`, `EntityTrackerAgent`, `SotaTrackerAgent`
- **`apps/pipeline`** — async orchestrator (`run_pipeline`) with bounded concurrency (Semaphore(3)); CLI entry point `mesh-pipeline`

End-to-end flow (see [agents.md](agents.md) for detail):

1. Scout fetches recent arxiv papers in configured categories
2. Dedup against DB by `raw_content_hash`; insert new Sources
3. Claim extractor runs in parallel (up to 3 concurrent) via local Ollama
4. Entity tracker resolves/creates entities for all extracted names
5. SOTA tracker synthesizes `achieves_score` claims into Beliefs
6. PipelineRun record written with counts and errors

Phase 1 explicitly excludes:

- A2A protocol layer (Phase 2)
- Embedding-based entity resolution (Phase 2)
- Skeptic agent (Phase 4)
- Web UI or API server
- Scheduling / cron

## Phase 2 — A2A Protocol (complete)

Phase 2 promotes each agent from a Python class to an A2A-compliant HTTP server. The in-process orchestrator is replaced by a coordinator that discovers agents via capability cards and dispatches by skill ID.

### Distributed mesh diagram

```
┌──────────────────────────────────────────────────────────────┐
│  Coordinator  (apps/pipeline/coordinator.py)                  │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │ 1. discover() → fetch agent cards from base URLs         │ │
│  │ 2. call_skill("scout_arxiv", {...})                       │ │
│  │ 3. call_skill("extract_claims", {paper})  [×N, bounded]  │ │
│  │ 4. call_skill("resolve_entities", {names, existing})     │ │
│  │ 5. call_skill("update_sota", {claims, existing_beliefs}) │ │
│  │ 6. All DB reads/writes via mesh-db                        │ │
│  └─────────────────────────────────────────────────────────┘ │
│                    ▼  JSON-RPC 2.0 (A2A)                      │
└──────┬────────────┬─────────────┬──────────────┬──────────────┘
       │            │             │              │
       ▼            ▼             ▼              ▼
 ┌──────────┐ ┌─────────┐ ┌──────────┐ ┌──────────────┐
 │ arxiv-   │ │ claim-  │ │ entity-  │ │ sota-        │
 │ scout    │ │ extractor│ │ tracker  │ │ tracker      │
 │ :8001    │ │ :8002   │ │ :8003    │ │ :8004        │
 │          │ │ (Ollama)│ │          │ │              │
 └──────────┘ └─────────┘ └──────────┘ └──────────────┘
```

Each agent:
- Exposes `GET /.well-known/agent-card.json` for discovery
- Handles `message/send` JSON-RPC calls at `/`
- Has `/healthz` for liveness checks
- Is a **pure function** (no DB access, no side effects)

The coordinator:
- Owns the DuckDB file (mounted as a volume)
- Pre-fetches DB context (existing entities, beliefs) and passes it to agents
- Persists all results after each skill call

See [docs/a2a.md](a2a.md) for full protocol documentation.

## Phase 3 — Read-Only Wiki (current)

Phase 3 makes the mesh legible. The accumulated knowledge — entities, claims,
beliefs, the revision timeline that proves "claims immutable, beliefs
mutable" — becomes a browsable web wiki, served by a thin Python read API in
front of DuckDB.

```
┌──────────────────────────────────────────────────────────────┐
│  apps/wiki  (Next.js 15, App Router)             :3000        │
│  Server components fetch via INTERNAL_API_URL (docker)        │
│  Browser fetches via NEXT_PUBLIC_API_URL (localhost)          │
└────────────────────────────┬─────────────────────────────────┘
                             │
┌────────────────────────────▼─────────────────────────────────┐
│  apps/api  (FastAPI, READ_ONLY DuckDB)           :8000        │
│  /healthz · /openapi.json · /docs                              │
│  /api/v1/{stats, pipeline-runs}                                │
│  /api/v1/{entities, claims, beliefs, sources}                  │
└────────────────────────────┬─────────────────────────────────┘
                             │  duckdb file (volume: mesh-data)
                             ▼
        ┌────────────────────────────────────────────┐
        │ DuckDB (single writer, many readers)        │
        └────────────────────────────────────────────┘
                             ▲
                             │  short batch writes only
                             │
       ┌──────────────────────────────────────────┐
       │  apps/pipeline coordinator (on demand)   │
       └──────────────────────────────────────────┘
                             ▲
                             │  A2A JSON-RPC
       ┌──────────────────────────────────────────┐
       │  arxiv-scout · claim-extractor · entity- │
       │  tracker · sota-tracker  (Phase 2)        │
       └──────────────────────────────────────────┘
```

Both `api` and `wiki` are long-running services brought up by `make up`.
The coordinator remains in the `pipeline` profile — invoked on demand by
`make pipeline`. See [docs/wiki.md](wiki.md) for the full Phase 3 narrative.

## Phase 4 — Falsification loop (complete)

Phase 4 turns the mesh into a self-revising system. Two new A2A agents join
the mesh and a new out-of-band orchestrator wires them together against
existing beliefs.

### Falsification loop diagram

```
┌──────────────────────────────────────────────────────────────────┐
│  skeptic_sweep  (apps/pipeline/skeptic_sweep.py, on demand)      │
│                                                                  │
│  1. Read held beliefs + derive last_challenged_at per belief     │
│  2. call_skill("select_beliefs_to_challenge", {beliefs, …})      │
│  3. Per pick: hydrate claims + entities,                         │
│     call_skill("challenge_belief", {belief, claims, …})          │
│  4. If verdict ∈ {weakened, contradicted}                        │
│     AND confidence ≥ MESH_SKEPTIC_APPLY_THRESHOLD:               │
│       - insert one Source (type=agent_reasoning)                 │
│       - insert counter-claims (extracted_by_agent=skeptic)       │
│       - update belief (confidence delta;                         │
│         contradicting_claim_ids if "contradicted")               │
│       - append BeliefRevision (revised_by_agent=skeptic)         │
│  5. Write PipelineRun row with run_type='skeptic_sweep'          │
│                                                                  │
└──────────┬────────────────────────────────┬──────────────────────┘
           │                                │  JSON-RPC 2.0 (A2A)
           ▼                                ▼
   ┌──────────────┐                  ┌──────────────┐
   │  curator     │                  │  skeptic     │
   │  :8007       │                  │  :8006       │
   │  pure / rule │                  │  LLM-backed  │
   └──────────────┘                  └──────────────┘
```

The main coordinator (`apps/pipeline/coordinator.py`) is **untouched** by
Phase 4. The scout / extract / synthesis flow runs as before; falsification
is a separate orchestrator with a separate Makefile target (`make skeptic`)
and a separate docker profile (`skeptic`). New scouts also drop in via the
existing `scout_*` skill-id prefix dispatch — Phase 4 added HN scout
(port 8005) without coordinator edits.

Key invariants preserved:

- **Claims immutable.** Skeptic produces *new* counter-claims with
  provenance; it never edits or deletes existing claims.
- **Provenance mandatory.** Every counter-claim points to a Source row;
  Skeptic synthesises one per assessment of `type=agent_reasoning` with
  `url=agent://skeptic/belief/<id>/<ts>`.
- **API stays read-only.** Phase 4 adds two new GET endpoints
  (`/api/v1/skeptic/recent`, `/api/v1/beliefs/{id}/revisions`) and a `/skeptic`
  wiki route. No write paths.

## Phase 5a — Async A2A foundation (complete)

Phase 5a is a plumbing change: the wire protocol between orchestrators and
agents moves from sync `send_message` (which blocked until the agent
finished) to a task-based submit-then-poll pattern. **No new features, no
new agents.** What changed:

- Each agent server now exposes `POST /mesh/tasks/submit` and
  `GET /mesh/tasks/{task_id}` (in addition to the existing agent card
  endpoint). The submit returns `202` with a `task_id` immediately; the
  actual work runs as a background `asyncio.create_task` whose status the
  agent tracks in an in-memory `TaskRegistry`.
- The coordinator and skeptic-sweep call `client.call_skill_blocking(...)`
  which submits the task and polls until completion. The orchestrator code
  shape (scout fan-out, extract semaphore, SOTA pass) is identical to
  Phase 4 — only the dispatch transport changed.
- The `AgentExecutor` + JSON-RPC handler scaffolding was removed from each
  agent module; skills are plain `async def _handle_<skill>(payload) -> dict`
  callables registered with `build_task_app`.

Task state lives only in agent process memory. If an agent restarts
mid-pipeline, the orchestrator's next poll returns 404 → `SkillCallError` and
the source records the failure; the pipeline keeps going. Durable task
storage is deferred to Phase 6 (alongside scheduling), per the locked
decision in the Phase 5 prompt.

See `docs/a2a.md` for the wire-level reference (request/response shapes,
env vars, lifecycle diagram).

## Package layout

```
packages/mesh-models   — Pydantic models; no I/O dependencies
packages/mesh-db       — DuckDB access; depends on mesh-models
packages/mesh-tracing  — Langfuse wrapper; no required dependencies
packages/mesh-llm      — Ollama client; depends on mesh-tracing
packages/mesh-agents   — Agent classes; depends on mesh-llm, mesh-db, mesh-models
packages/mesh-a2a      — A2A client + card builder; depends on mesh-tracing
apps/cli               — Click CLI; depends on mesh-db, mesh-models, mesh-llm
apps/pipeline          — Async orchestrator + coordinator; depends on mesh-agents, mesh-a2a
apps/agents            — A2A agent server entry points
apps/api               — FastAPI read service; depends on mesh-db, mesh-models   (Phase 3)
apps/wiki              — Next.js 15 wiki; consumes apps/api via OpenAPI         (Phase 3)
```

Dependencies flow strictly downward. `mesh-models` has no internal dependencies.

## Database design decisions

See [schema.md](schema.md) for full rationale. Key decisions:

1. **Single DuckDB file** — appropriate for Phase 0 and single-node Phase 1. Avoids infra overhead. Path configurable via `MESH_DB_PATH`.
2. **Claims immutable by design** — enforced at the access layer (no `update_claim()` function exists). Only `update_claim_status()` is allowed.
3. **Arrays stored as DuckDB native arrays** — cleaner than JSON arrays for list fields like `aliases`, `supporting_claim_ids`.
4. **JSON for flexible dicts** — `attributes` and `object` stored as JSON strings, parsed on read.
5. **VSS installed early** — the `name_embedding` column on entities is inert in Phase 0 but positions us for entity resolution in Phase 2 without a schema migration.
