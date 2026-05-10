# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Agent Mesh is a persistent multi-agent system for tracking AI/robotics research. The system maintains a living knowledge base built from structured **Claims** (immutable, extracted from sources) synthesized into mutable **Beliefs**. Currently in **Phase 1** — end-to-end pipeline: arxiv → Ollama claim extraction → entity tracking → SOTA beliefs. No A2A protocol yet.

## Commands

```bash
# Setup
uv sync
cp .env.example .env
uv run mesh.cli init-db        # creates ./data/mesh.db, applies migrations

# Run the pipeline (requires Ollama running locally)
uv run mesh-pipeline
uv run mesh-pipeline --categories cs.LG --max-papers 50 --since 7d

# Inspect pipeline output
uv run mesh.cli pipeline-stats
uv run mesh.cli show-sota-beliefs
uv run mesh.cli show-recent-claims
uv run mesh.cli ollama-check

# Tests (no Ollama needed — uses MockOllamaClient)
uv run pytest
uv run pytest tests/test_orchestrator.py   # single file

# Lint / type check
uv run ruff check .
uv run ruff check . --fix
uv run mypy .
```

CI runs `ruff check`, `mypy`, and `pytest -v` on every push.

## Architecture

This is a `uv` workspace monorepo. Dependency flow is strictly one-way:

```
mesh-models  ←  mesh-db  ←  mesh-agents  ←  apps/pipeline
mesh-tracing  ←  mesh-llm  ←  mesh-agents
apps/cli  (depends on mesh-db, mesh-models, mesh-llm)
```

- **`packages/mesh-models`** — Pydantic v2 domain models; no I/O. Seven entities: `Entity`, `Source`, `Claim`, `Belief`, `BeliefRevision`, `Relationship`, `Investigation`.
- **`packages/mesh-db`** — DuckDB access layer. One typed module per entity (`entities.py`, `claims.py`, etc.). Migrations in `packages/mesh-db/migrations/NNN_description.sql`, applied via `apply_migrations()` which is idempotent.
- **`packages/mesh-tracing`** — Langfuse wrapper; no-ops when env vars are absent.
- **`packages/mesh-llm`** — `OllamaClient` with structured output (`format=model.model_json_schema()`), retry via tenacity, and `complete_with_latency()`. `LLMResponseError` signals parse failure (pipeline continues); `OllamaNotReadyError` signals connection failure (pipeline aborts).
- **`packages/mesh-agents`** — Four agent classes, each with `async run(input) -> output`. `ClaimExtractorAgent` calls Ollama; `EntityTrackerAgent` does find-or-create against DB; `SotaTrackerAgent` is rule-based (no LLM).
- **`apps/cli`** — Click CLI (`mesh.cli`) wrapping all DB operations with `rich` table output.
- **`apps/pipeline`** — Async orchestrator (`run_pipeline`). Bounded concurrency via `asyncio.Semaphore(MESH_PIPELINE_CONCURRENCY)`. One bad paper records an error and continues; Ollama connection failure aborts.

## Key invariants

- **Claims are immutable**: no `update_claim()` exists. Only `update_claim_status()` is allowed. If new evidence supersedes a claim, insert a new claim and mark the old one `superseded`.
- **BeliefRevisions are append-only**: every belief change writes a revision row; never update or delete revision rows.
- **Database path** is controlled by `MESH_DB_PATH` env var (default `./data/mesh.db`). Tests should use an in-memory or temp-file DB to avoid polluting the dev DB.
- **`name_embedding FLOAT[384]`** exists on the `entities` table but is intentionally unpopulated in Phase 1 (reserved for Phase 2 VSS entity resolution).
- **DuckDB array ops**: use `list_filter(arr, x -> condition)` — never `cardinality()` which is for MAP types only.

## Adding a migration

1. Create `packages/mesh-db/migrations/NNN_description.sql`
2. Run `uv run mesh.cli init-db` — applies only unapplied migrations

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MESH_DB_PATH` | `./data/mesh.db` | DuckDB file path |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `MESH_LLM_MODEL` | `qwen3:8b` | Model for claim extraction |
| `MESH_PIPELINE_CATEGORIES` | `cs.AI,cs.RO,cs.LG` | Default arxiv categories |
| `MESH_PIPELINE_MAX_PAPERS` | `20` | Papers per pipeline run |
| `MESH_PIPELINE_CONCURRENCY` | `3` | Parallel LLM slots |
| `LANGFUSE_PUBLIC_KEY` | (empty) | Enables tracing if set |
| `LANGFUSE_SECRET_KEY` | (empty) | Required alongside public key |
| `LANGFUSE_HOST` | `http://localhost:3000` | Langfuse server |

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
  uv run mesh-pipeline --help
  ```
