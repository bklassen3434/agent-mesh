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

## Phase 0 — Foundation (current)

Phase 0 establishes the substrate. It includes:

- **Repository structure** — uv workspace monorepo with `packages/` and `apps/`
- **Pydantic v2 models** — typed representations of all seven domain entities
- **DuckDB schema** — migrations for all tables; single file database at `./data/mesh.db`
- **Database access layer** — typed read/write functions for each entity; immutability enforced on Claims
- **CLI** — `mesh.cli` with subcommands to create and inspect all entity types
- **Tracing plumbing** — Langfuse wrapper that no-ops without env vars
- **Test suite** — model validation, migration, DB round-trip, and CLI tests
- **CI** — GitHub Actions running ruff, mypy, pytest

Phase 0 explicitly excludes:

- Any agents (scouts, extractors, synthesizers, skeptics, curators)
- Any LLM API calls
- Any A2A protocol code
- Any web UI or API server
- Any external data fetching or scheduling
- Vector embedding generation (VSS extension installed, column added, but no population)

## Package layout

```
packages/mesh-models   — Pydantic models; no I/O dependencies
packages/mesh-db       — DuckDB access; depends on mesh-models
packages/mesh-tracing  — Langfuse wrapper; no required dependencies
apps/cli               — Click CLI; depends on mesh-db and mesh-models
```

Dependencies flow strictly downward. `mesh-models` has no internal dependencies; `mesh-db` depends only on `mesh-models`.

## Database design decisions

See [schema.md](schema.md) for full rationale. Key decisions:

1. **Single DuckDB file** — appropriate for Phase 0 and single-node Phase 1. Avoids infra overhead. Path configurable via `MESH_DB_PATH`.
2. **Claims immutable by design** — enforced at the access layer (no `update_claim()` function exists). Only `update_claim_status()` is allowed.
3. **Arrays stored as DuckDB native arrays** — cleaner than JSON arrays for list fields like `aliases`, `supporting_claim_ids`.
4. **JSON for flexible dicts** — `attributes` and `object` stored as JSON strings, parsed on read.
5. **VSS installed early** — the `name_embedding` column on entities is inert in Phase 0 but positions us for entity resolution in Phase 2 without a schema migration.
