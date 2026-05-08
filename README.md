# Agent Mesh

A persistent multi-agent system that tracks AI/robotics research through a network of A2A-protocol agents.

**Status: Phase 0 — Foundation** (schema, DB layer, CLI skeleton; no agents yet)

## Quick start

```bash
git clone <repo>
cd agent-mesh
uv sync
cp .env.example .env
uv run mesh.cli init-db
uv run mesh.cli add-entity --name "GR00T-N1" --type model
uv run mesh.cli show-entities
uv run pytest
```

## Docs

- [Schema](docs/schema.md) — data model and design rationale
- [Architecture](docs/architecture.md) — system context and phase roadmap
- [Development](docs/development.md) — contributor setup guide
