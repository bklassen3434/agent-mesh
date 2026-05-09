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
| `MESH_LLM_MODEL` | `qwen3:14b` | Model for claim extraction |
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
| `MESH_LLM_MODEL` | `qwen3:14b` | Model for claim extraction |
| `MESH_PIPELINE_CATEGORIES` | `cs.AI,cs.RO,cs.LG` | Default arxiv categories |
| `MESH_PIPELINE_MAX_PAPERS` | `20` | Papers per pipeline run |
| `MESH_PIPELINE_CONCURRENCY` | `3` | Parallel LLM slots |
| `LANGFUSE_PUBLIC_KEY` | (empty) | Enables Langfuse tracing if set |
| `LANGFUSE_SECRET_KEY` | (empty) | Required alongside public key |
| `LANGFUSE_HOST` | `http://localhost:3000` | Langfuse server URL |

Tracing is a no-op if the Langfuse keys are absent — you do not need a Langfuse instance to develop.

## Adding a new migration

1. Create `packages/mesh-db/migrations/NNN_description.sql`
2. Run `uv run mesh.cli init-db` — it will apply only the new migration
3. The migration runner is idempotent; running it on an already-migrated DB is safe
