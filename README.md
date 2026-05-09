# Agent Mesh

A persistent multi-agent system that tracks AI/robotics research through a network of A2A-protocol agents.

**Status: Phase 1 complete** — pipeline ingests arxiv papers, extracts claims via local LLM (Ollama + Qwen3), tracks SOTA. Fully offline-capable. No A2A yet.

## Quick start

```bash
git clone <repo>
cd agent-mesh
uv sync
cp .env.example .env
uv run mesh.cli init-db

# Run the pipeline (requires Ollama — see docs/llm-setup.md)
ollama pull qwen3:14b
uv run mesh-pipeline

# Inspect results
uv run mesh.cli show-sota-beliefs
uv run mesh.cli show-recent-claims

# Run tests (no Ollama needed)
uv run pytest
```

## Docs

- [Schema](docs/schema.md) — data model and design rationale
- [Architecture](docs/architecture.md) — system context and phase roadmap
- [Development](docs/development.md) — contributor setup guide
- [LLM Setup](docs/llm-setup.md) — Ollama installation, model recommendations, troubleshooting
- [Agents](docs/agents.md) — agent catalogue and orchestrator flow
