# Agent Mesh

A persistent multi-agent system that tracks AI/robotics research through a network of A2A-protocol agents, with a read-only web wiki for browsing what the mesh has learned.

**Status: Phase 4 complete** — Phases 0–3 cover substrate, end-to-end
pipeline, A2A protocol promotion, and the read API + Next.js wiki with the
revision timeline. Phase 4 adds the **falsification loop**: a Skeptic agent
that challenges existing beliefs and emits counter-claims, a Curator that
picks which beliefs are worth challenging, an out-of-band `make skeptic`
sweep that wires them together via A2A, and a `/skeptic` wiki feed that
surfaces the activity. The HN scout also lands in Phase 4 alongside the
arxiv scout.

## Quick start

```bash
git clone <repo>
cd agent-mesh
uv sync
cp .env.example .env

# Bring up the full stack: four agents + read API + wiki.
make up
make wiki              # opens http://localhost:3000
make api               # opens http://localhost:8000/docs

# Run one pipeline cycle against arxiv + HN to populate the mesh.
ollama pull qwen3:8b
make pipeline

# (Phase 4) Run one out-of-band falsification sweep — Curator picks beliefs,
# Skeptic challenges them, counter-claims + revisions land in the DB.
make skeptic

# Inspect via CLI (still supported).
uv run mesh.cli show-sota-beliefs
uv run mesh.cli show-recent-claims

# Run tests (no Ollama needed).
uv run pytest
cd apps/wiki && npm run lint && npm run typecheck && npm run build
```

## What's there to click on

After a pipeline run, the wiki at <http://localhost:3000> has:

- **Home** — stat tiles, recent pipeline runs, most-recently-revised beliefs.
- **Entities** — paginated browser with type + name filters.
- **Beliefs / [id]** — the headline view: supporting and contradicting claims
  with excerpts and source links, plus a vertical **revision timeline** that
  shows how each belief changed over time, what claims triggered the change,
  and the rationale logged for each revision. This is the visual proof that
  claims are immutable and beliefs are mutable.
- **Claims**, **Sources** — drill-down browsers for the raw provenance graph.

A typed JSON contract sits underneath at <http://localhost:8000/docs>.
Next.js is just one consumer — any future client speaks the same JSON.

## Docs

- [Schema](docs/schema.md) — data model and design rationale
- [Architecture](docs/architecture.md) — system context and phase roadmap
- [Wiki (Phase 3)](docs/wiki.md) — why API-in-front-of-DuckDB, read-only
  coexistence with the coordinator, the Pydantic → OpenAPI → TypeScript pipeline
- [A2A](docs/a2a.md) — Phase 2 protocol documentation
- [Development](docs/development.md) — contributor setup guide, wiki dev workflow
- [LLM Setup](docs/llm-setup.md) — Ollama installation, model recommendations
- [Agents](docs/agents.md) — agent catalogue and orchestrator flow

## Screenshots

_TODO: add screenshots of the home dashboard and a belief detail page with the revision timeline._
