# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Agent Mesh is a persistent multi-agent system for tracking AI/robotics research. The system maintains a living knowledge base built from structured **Claims** (immutable, extracted from sources) synthesized into mutable **Beliefs**.

**Phase status:** Phases 0–2 complete (substrate, end-to-end pipeline, A2A protocol promotion). **Phase 3 in progress** — a read-only FastAPI service (`apps/api`, :8000) in front of DuckDB and a Next.js wiki (`apps/wiki`, :3000) that renders entities, claims, beliefs with full provenance, and the revision timeline. Both new services come up with `make up` alongside the four A2A agents.

## Commands

```bash
# Setup
uv sync
cp .env.example .env
uv run mesh.cli init-db        # creates ./data/mesh.db, applies migrations

# Run the pipeline (defaults to Anthropic Claude Haiku 4.5; needs ANTHROPIC_API_KEY in .env. Switch to local Ollama with MESH_LLM_PROVIDER=ollama)
uv run mesh-pipeline
uv run mesh-pipeline --categories cs.LG --max-papers 50 --since 7d

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
- **`packages/mesh-db`** — DuckDB access layer. One typed module per entity (`entities.py`, `claims.py`, etc.). Migrations in `packages/mesh-db/migrations/NNN_description.sql`, applied via `apply_migrations()` which is idempotent.
- **`packages/mesh-tracing`** — Langfuse wrapper; no-ops when env vars are absent.
- **`packages/mesh-llm`** — Two interchangeable LLM clients implementing the `LLMClient` Protocol: `AnthropicClient` (default; `messages.parse()` for Pydantic-typed structured output with `cache_control` on the system prompt) and `OllamaClient` (local; structured output via `format=schema`). `make_llm_client()` picks one based on `MESH_LLM_PROVIDER`. `LLMResponseError` signals parse failure (pipeline continues); `AnthropicNotReadyError` / `OllamaNotReadyError` signal provider failure (pipeline aborts).
- **`packages/mesh-agents`** — Four agent classes, each with `async run(input) -> output`. `ClaimExtractorAgent` calls the configured LLM via `LLMClient`; `EntityTrackerAgent` does find-or-create against DB; `SotaTrackerAgent` is rule-based (no LLM).
- **`apps/cli`** — Click CLI (`mesh.cli`) wrapping all DB operations with `rich` table output.
- **`apps/pipeline`** — Async orchestrator (`run_pipeline`). Bounded concurrency via `asyncio.Semaphore(MESH_PIPELINE_CONCURRENCY)`. One bad paper records an error and continues; LLM-provider failure aborts.
- **`apps/api`** (Phase 3) — FastAPI read-only HTTP service on :8000. One read-only DuckDB connection per request via a FastAPI dependency. Endpoints under `/api/v1/`; OpenAPI at `/openapi.json`, Swagger UI at `/docs`. Applies migrations once at startup against a brief read-write open; all request handling is read-only.
- **`apps/wiki`** (Phase 3) — Next.js 15 App Router wiki on :3000. Server components only (no client state libs); Tailwind + hand-written shadcn-style primitives. TypeScript types live in `apps/wiki/src/lib/api-types.ts`, generated from the API's OpenAPI spec by `openapi-typescript`. CI regenerates and diffs to detect drift.

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
| `MESH_LLM_PROVIDER` | `anthropic` | `anthropic` (cloud, Haiku 4.5) or `ollama` (local) |
| `MESH_LLM_MODEL` | `claude-haiku-4-5` | Model ID; matches the provider |
| `ANTHROPIC_API_KEY` | (empty) | Required when provider=anthropic |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL (provider=ollama only) |
| `MESH_PIPELINE_CATEGORIES` | `cs.AI,cs.RO,cs.LG` | Default arxiv categories |
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
## Commit policy
- Create a commit after each logical unit of work (feature, bugfix, refactor step).
- Use Conventional Commits: `feat:`, `fix:`, `refactor:`, `chore:`, `docs:`, `test:`.
- Stage with `git add -A` only after reviewing what changed.
- Do not push to remote unless explicitly asked.
- Skip commits if the working tree is clean.
