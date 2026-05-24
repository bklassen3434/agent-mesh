# Development Guide

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- [Ollama](https://ollama.com) with a pulled model (for pipeline runs — not needed for tests)

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

This creates `./data/mesh.db` and applies all migrations.

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

## Running the pipeline

The pipeline requires Ollama running locally. See [llm-setup.md](llm-setup.md) for installation.

```bash
# Run with defaults (cs.AI, cs.RO, cs.LG; last 24h; max 20 papers)
uv run mesh-pipeline

# Fetch up to 50 papers from cs.LG in the last 7 days
uv run mesh-pipeline --categories cs.LG --max-papers 50 --since 7d

# Use a specific DB file
uv run mesh-pipeline --db-path /tmp/research.db

# Check results
uv run mesh.cli pipeline-stats
uv run mesh.cli show-sota-beliefs
uv run mesh.cli show-recent-claims
uv run mesh.cli ollama-check
```

Key environment variables (in `.env`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `MESH_LLM_MODEL` | `qwen3:8b` | Model for claim extraction |
| `MESH_PIPELINE_CATEGORIES` | `cs.AI,cs.RO,cs.LG` | Default arxiv categories |
| `MESH_PIPELINE_MAX_PAPERS` | `20` | Papers per run |
| `MESH_PIPELINE_CONCURRENCY` | `3` | Parallel LLM extraction slots |

## Run tests

```bash
uv run pytest
uv run pytest -v           # verbose
uv run pytest tests/test_models.py  # single file
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
│   └── pipeline/          — mesh-pipeline orchestrator
├── packages/
│   ├── mesh-models/       — Pydantic v2 domain models
│   ├── mesh-db/           — DuckDB access layer + migrations
│   ├── mesh-tracing/      — Langfuse tracing wrapper
│   ├── mesh-llm/          — Ollama client + prompts
│   └── mesh-agents/       — Agent classes (scout, extractor, tracker, synthesizer)
├── tests/                 — pytest test suite
└── docs/                  — this directory
```

## Environment variables

Copy `.env.example` to `.env` and fill in as needed:

| Variable | Default | Purpose |
|----------|---------|---------|
| `MESH_DB_PATH` | `./data/mesh.db` | Path to DuckDB file |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `MESH_LLM_MODEL` | `qwen3:8b` | Model for claim extraction |
| `MESH_PIPELINE_CATEGORIES` | `cs.AI,cs.RO,cs.LG` | Default arxiv categories |
| `MESH_PIPELINE_MAX_PAPERS` | `20` | Papers per pipeline run |
| `MESH_PIPELINE_CONCURRENCY` | `3` | Parallel LLM slots |
| `LANGFUSE_PUBLIC_KEY` | (empty) | Enables Langfuse tracing if set |
| `LANGFUSE_SECRET_KEY` | (empty) | Required alongside public key |
| `LANGFUSE_HOST` | `http://localhost:3000` | Langfuse server URL |

Tracing is a no-op if the Langfuse keys are absent — you do not need a Langfuse instance to develop.

## Running the distributed stack (Phase 2)

Phase 2 runs each agent as a separate HTTP server coordinated by the coordinator.

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

# Build and start agent services
make up

# Run one full pipeline cycle
make pipeline

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

### Agent ports

| Agent | Port | Skill |
|-------|------|-------|
| arxiv-scout | 8001 | `scout_arxiv` |
| claim-extractor | 8002 | `extract_claims` |
| entity-tracker | 8003 | `resolve_entities` |
| sota-tracker | 8004 | `update_sota` |

### New environment variables (Phase 2)

| Variable | Default | Purpose |
|----------|---------|---------|
| `MESH_USE_A2A` | `false` | Set to `true` to use A2A coordinator instead of Phase 1 orchestrator |
| `MESH_AGENT_URLS` | localhost ports 8001-8004 | Comma-separated agent base URLs for discovery |
| `AGENT_HOST` | `0.0.0.0` | Bind address for agent servers |
| `AGENT_PORT` | varies | Port for agent server |
| `AGENT_PUBLIC_URL` | `http://<name>:<port>` | URL advertised in the Agent Card |

## Adding a new migration

1. Create `packages/mesh-db/migrations/NNN_description.sql`
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
make up            # four agents + api (:8000) + wiki (:3000)
make pipeline      # one-shot coordinator run; populates the DB
make wiki          # opens the home dashboard
make down
```
