# Development Guide

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- An LLM provider for pipeline runs (not needed for tests): an `ANTHROPIC_API_KEY` (default provider) or a local [Ollama](https://ollama.com) with a pulled model. See [llm-setup.md](llm-setup.md).
- Postgres: a single `pgvector/pgvector:pg16` instance (`mesh-postgres`) backs the whole store. `make up` brings it up in docker; tests start their own ephemeral container (see below).

## Setup (under 5 minutes)

```bash
git clone <repo>
cd agent-mesh
uv sync
cp .env.example .env
```

## Initialize the database

```bash
uv run mesh.cli init-db
```

This applies the Postgres knowledge schema + roles (uses MESH_PG_URL / LANGGRAPH_POSTGRES_URL).
It runs the numbered SQL migrations in `packages/mesh-db/migrations_pg/NNN_*.sql` and is idempotent —
only unapplied migrations run. DuckDB is no longer used (removed in Phase 12); the store is a single
pgvector Postgres instance.

## Try the CLI

```bash
# Add an entity
uv run mesh.cli add-entity --name "GR00T-N1" --type model

# Add an entity with aliases and attributes
uv run mesh.cli add-entity \
  --name "GPT-4" \
  --type model \
  --alias "gpt4" \
  --alias "openai-gpt4" \
  --attribute "context_length=128000"

# Add a source
uv run mesh.cli add-source \
  --type arxiv \
  --url "https://arxiv.org/abs/2303.08774" \
  --published-at "2023-03-15T00:00:00"

# Show entities
uv run mesh.cli show-entities
uv run mesh.cli show-entities --type model

# Inspect any record by ID
uv run mesh.cli inspect <id>

# Add a belief
uv run mesh.cli add-belief \
  --topic "llm-scaling" \
  --statement "Scaling model size improves benchmark performance." \
  --confidence 0.85

# Revise a belief
uv run mesh.cli add-revision \
  --belief <belief-id> \
  --new-statement "Scaling laws apply up to ~1T parameters with current architectures." \
  --new-confidence 0.75 \
  --rationale "Chinchilla paper suggests data-optimal scaling matters more than raw size"

# Show revision history
uv run mesh.cli show-revisions --belief <belief-id>
```

## Running the pipeline (the controller)

The deterministic controller (`mesh-controller`) is the only orchestration job — it
runs the whole reactive loop (scout → extract → resolve → consolidate → synthesize →
challenge → investigate). It needs an LLM provider — by default Anthropic
(`ANTHROPIC_API_KEY` in `.env`); set `MESH_LLM_PROVIDER=ollama` to run against a
local Ollama instead. See [llm-setup.md](llm-setup.md).

```bash
# Shadow: preview one round's plan + effects, write nothing
uv run mesh-controller

# Act + loop to quiescence
uv run mesh-controller --apply

# Scope a run to a specific field
uv run mesh-controller --apply --field ai-robotics

# Check results
uv run mesh.cli pipeline-stats
uv run mesh.cli show-sota-beliefs
uv run mesh.cli show-recent-claims
uv run mesh.cli ollama-check
```

Key environment variables (in `.env`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `MESH_LLM_PROVIDER` | `anthropic` | `anthropic` (cloud) or `ollama` (local) |
| `MESH_LLM_MODEL` | `claude-haiku-4-5` | Model for claim extraction (matches the provider) |
| `ANTHROPIC_API_KEY` | (empty) | Required when provider is `anthropic` |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL (provider=ollama only) |
| `MESH_PIPELINE_FIELD` | `ai-robotics` | Field slug a run scopes to (`--field`) |
| `MESH_PIPELINE_CATEGORIES` | (field's arxiv connector config) | Optional per-run arxiv category override |
| `MESH_PIPELINE_MAX_PAPERS` | `20` | Papers per run |
| `MESH_PIPELINE_CONCURRENCY` | `3` | Parallel LLM extraction slots |

## Run tests

Tests need no LLM. They spin up an ephemeral `pgvector/pgvector:pg16` container via testcontainers
and apply the schema with `init_pg` (see `tests/conftest.py`) — never point them at a real DB. Docker
must be running.

```bash
uv run pytest
uv run pytest -v           # verbose
uv run pytest tests/test_models.py  # single file
```

If port-mapping fails with a Ryuk error, disable the reaper:

```bash
TESTCONTAINERS_RYUK_DISABLED=true uv run pytest
```

## Lint and type check

```bash
uv run ruff check .
uv run ruff check . --fix   # auto-fix
uv run mypy .
```

## Project structure

```
agent-mesh/
├── apps/
│   ├── cli/               — mesh.cli entry point
│   ├── pipeline/          — deterministic controller (mesh-controller)
│   ├── agents/            — A2A agent HTTP servers (orphaned; skills call core fns in-process)
│   ├── api/               — read-only FastAPI service (:8000)
│   ├── wiki/              — Next.js wiki (:3000)
│   └── scheduler/         — BackgroundScheduler control surface (:9100)
├── packages/
│   ├── mesh-models/       — Pydantic v2 domain models
│   ├── mesh-db/           — Postgres access layer (psycopg pool) + migrations_pg/
│   ├── mesh-tracing/      — Langfuse tracing wrapper
│   ├── mesh-llm/          — Anthropic + Ollama clients, routing, prompts
│   └── mesh-agents/       — Agent classes (scout, extractor, tracker, synthesizer, …)
├── tests/                 — pytest test suite
└── docs/                  — this directory (see agents.md for the full agent fleet)
```

## Environment variables

Copy `.env.example` to `.env` and fill in as needed:

| Variable | Default | Purpose |
|----------|---------|---------|
| `MESH_PG_URL` | (falls back to `LANGGRAPH_POSTGRES_URL`) | Knowledge-store Postgres DSN (owner; used for migrations) |
| `MESH_LLM_PROVIDER` | `anthropic` | `anthropic` (cloud) or `ollama` (local) |
| `MESH_LLM_MODEL` | `claude-haiku-4-5` | Model for claim extraction (matches the provider) |
| `ANTHROPIC_API_KEY` | (empty) | Required when provider is `anthropic` |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL (provider=ollama only) |
| `MESH_PIPELINE_MAX_PAPERS` | `20` | Papers per pipeline run |
| `MESH_PIPELINE_CONCURRENCY` | `3` | Parallel LLM slots |
| `LANGFUSE_PUBLIC_KEY` | (empty) | Enables Langfuse tracing if set |
| `LANGFUSE_SECRET_KEY` | (empty) | Required alongside public key |
| `LANGFUSE_HOST` | `http://localhost:3000` | Langfuse server URL |

The full environment-variable reference (routing, confidence, consolidation, discovery, observability,
etc.) lives in the repo root `CLAUDE.md`.

Tracing is a no-op if the Langfuse keys are absent — you do not need a Langfuse instance to develop.

## Running the distributed stack (Phase 2, legacy)

> Historical: the A2A agent servers still exist but are orphaned — the controller's
> skills call the shared core functions in-process. The compose services below boot
> the servers, but the production path no longer dispatches through them.

### Prerequisites (additional)

- [Docker](https://docs.docker.com/get-docker/) + Docker Compose v2
- Ollama accessible from containers (either `host.docker.internal` or a dedicated Ollama compose service)

### Ollama and Docker

Ollama runs **on the host machine**, not inside the compose stack. The
`claim-extractor` container must reach it over the Docker virtual network.

**Mac / Windows (Docker Desktop)** — works out of the box. The compose file
defaults `OLLAMA_HOST` to `http://host.docker.internal:11434`, which Docker
Desktop resolves to the host automatically. No extra configuration needed.

**Linux** — `host.docker.internal` is not available by default. Set
`OLLAMA_HOST` in your `.env` to the Docker bridge gateway address:

```bash
echo 'OLLAMA_HOST=http://172.17.0.1:11434' >> .env
```

To verify Ollama is reachable from inside the container after `make up`:

```bash
docker compose exec claim-extractor curl -s http://host.docker.internal:11434/api/tags
```

A JSON response listing models confirms connectivity. If it times out, check
that Ollama is running (`ollama serve`) and that `OLLAMA_HOST` points to the
correct address for your platform.

### Quick start

```bash
# Copy env — no OLLAMA_HOST override needed on Mac/Windows
cp .env.example .env
# Linux only: point containers at the Docker bridge gateway
# echo 'OLLAMA_HOST=http://172.17.0.1:11434' >> .env

# Build and start services
make up

# Run one full controller cycle
make controller-apply

# Show pipeline stats
uv run mesh.cli pipeline-stats --last 1

# Discover running agents
uv run mesh.cli a2a-discover

# Call a skill manually (for debugging)
uv run mesh.cli a2a-call resolve_entities '{"candidate_names": ["GPT-4"], "existing_entities": []}'

# Tear down
make down
```

### Manual smoke test

```bash
make smoke
```

This brings up the full stack, runs one pipeline cycle, checks DB row counts, and verifies A2A discovery.

### Running a single agent in isolation

```bash
# Entity tracker on port 8003
AGENT_PORT=8003 AGENT_PUBLIC_URL=http://localhost:8003 \
  uv run python -m mesh_agent_servers.entity_tracker

# In another terminal — call the skill:
uv run mesh.cli a2a-call resolve_entities \
  '{"candidate_names": ["GPT-4"], "existing_entities": []}' \
  --agent-urls http://localhost:8003
```

The agent HTTP servers live in `apps/agents/src/mesh_agent_servers/`.

### Agent fleet

The fleet has grown well beyond the original four agents: ~10 scouts (arxiv, hn, github,
bluesky, reddit, blog, leaderboard, web-search, rss, rest-json) plus worker agents
(claim-extractor, entity-tracker, sota-tracker, curator, skeptic, personalizer,
research-qa). These A2A servers are now orphaned — the deterministic controller's skills
call the same core functions in-process, so no fleet needs to run. See [agents.md](agents.md)
for the roster, ports, and skills, and the `docker-compose.yml` services list for what
`make up` boots.

### Orchestration: the deterministic controller

The deterministic controller (`mesh-controller`) is the only orchestrator — there is no
coordinator/ingest graph or legacy in-process orchestrator anymore (both deleted, along with
the `--a2a` flag). The controller senses the store into tensions, plans the round via the rule
table, and dispatches skills that emit effects through the write gateway. See
[deterministic-controller.md](deterministic-controller.md).

### Agent-server environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `MESH_AGENT_URLS` | localhost agent ports | Comma-separated agent base URLs for discovery |
| `AGENT_HOST` | `0.0.0.0` | Bind address for agent servers |
| `AGENT_PORT` | varies | Port for agent server |
| `AGENT_PUBLIC_URL` | `http://<name>:<port>` | URL advertised in the Agent Card |

## Adding a new migration

1. Create `packages/mesh-db/migrations_pg/NNN_description.sql` (Postgres DDL, `knowledge` schema)
2. Run `uv run mesh.cli init-db` — it will apply only the new migration
3. The migration runner is idempotent; running it on an already-migrated DB is safe

## Wiki dev workflow (Phase 3)

Two terminals:

```bash
# Terminal 1 — read API
uv run mesh-api                  # :8000; /docs for Swagger

# Terminal 2 — wiki
cd apps/wiki
npm install                      # once
npm run dev                      # :3000 with hot reload
```

Open <http://localhost:3000> for the wiki and <http://localhost:8000/docs>
for the Swagger UI.

### Regenerating TypeScript types

`apps/wiki/src/lib/api-types.ts` is generated from the API's `/openapi.json`.
After any API contract change:

```bash
make types         # equivalent to (cd apps/wiki && npm run generate-types)
```

CI regenerates and diffs to catch drift. See [docs/wiki.md](wiki.md) for the
full architectural rationale.

### Running the full stack in docker

```bash
make up                # api + wiki + scheduler + mesh-postgres
make controller        # one shadow controller round (previews the plan, writes nothing)
make controller-apply  # one controller run; acts + loops to quiescence; populates the DB
make wiki              # opens the wiki dashboard
make api               # opens the Swagger UI
make types             # regenerate apps/wiki/src/lib/api-types.ts (needs API up)
make test              # uv run pytest + wiki Playwright E2E (make test-ui)
make down              # tear down
```

`make test-ui-headed` / `test-ui-debug` / `test-ui-report` drive the wiki Playwright E2E
in headed / debug / report modes.

## Falsification (the challenge-belief rule)

Belief challenge is now a controller rule, not a separate sweep job: the
`challenge-contested-beliefs` rule routes `contested_claim` / `stale_belief`
tensions to the `challenge-belief` skill. Curator picks beliefs worth challenging,
the skeptic assesses each, and applicable assessments land as counter-claims plus
BeliefRevisions in the DB — all in a normal controller run (`mesh-controller
--apply`).

### Falsification environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MESH_SKEPTIC_APPLY_THRESHOLD` | `0.7` | Apply an assessment only if the Skeptic's self-reported confidence clears this. |
| `MESH_CURATOR_PICK_COUNT` | `5` | How many beliefs Curator returns per challenge pass. |
| `MESH_CURATOR_COOLDOWN_DAYS` | `7` | Beliefs the skeptic looked at within this window get a Curator score penalty so they don't dominate back-to-back runs. |
| `MESH_SKEPTIC_SOURCE_RELIABILITY` | `0.4` | `reliability_prior` on the synthetic `agent_reasoning` source rows the skeptic emits. |
| `MESH_LLM_MODEL_SKEPTIC` | (unset) | Per-agent model override for the skeptic (see [llm-setup.md](llm-setup.md) for the routing precedence). |
