# Agent Mesh

A persistent, **field-agnostic** multi-agent system that tracks a research
field through a network of A2A-protocol agents, synthesizes a living knowledge
base, and serves it through a Next.js wiki. It ships seeded for AI/robotics,
but the core never branches on the field — point it at a new field and the same
pipeline applies.

The interesting bits:

- **Field-agnostic by construction.** A first-class **Field** scopes all
  state (entities, sources, claims, beliefs, relationships, investigations,
  memory, schedules). The three coupled system prompts are profile-driven
  builders; entity/belief resolution and memory never cross fields. `ai-robotics`
  is just the seeded default. See [`docs/field-agnostic.md`](docs/field-agnostic.md).
- **Falsification-first.** A `Skeptic` agent challenges beliefs that have
  been around long enough or are thin enough to deserve it. Counter-claims are
  first-class data with a structured `failure_mode` taxonomy
  (`methodological_flaw`, `cherry_picked_evidence`, …). See [`docs/posts/falsification-first.md`](docs/posts/falsification-first.md).
- **Semantic resolution, conservatively.** Entities de-duplicate by embedding
  similarity (block → match → merge, with an LLM adjudicating the middle band);
  beliefs consolidate the same way but **strictly append-only** — a merged-away
  belief is absorbed, never erased. See [`docs/entity-resolution.md`](docs/entity-resolution.md)
  and [`docs/belief-consolidation.md`](docs/belief-consolidation.md).
- **Synthesis beyond leaderboards.** Every claim carries a `claim_type`;
  synthesis produces SOTA beliefs, entity-anchored capability beliefs, and
  claim-grounded graph edges. Belief confidence is derived from evidence
  signals (source diversity, reproduction, skeptic attacks), never hardcoded.
  See [`docs/belief-synthesis.md`](docs/belief-synthesis.md).
- **Autonomous discovery.** The mesh analyzes its own field for gaps
  (under-evidenced entities, thin/stale beliefs, rising topics) and opens its
  own investigations — proposing evidence-gathering, never facts. See
  [`docs/autonomous-discovery.md`](docs/autonomous-discovery.md).
- **Tiered model routing.** Requests run on a cheap model by default and
  escalate to a strong one on a pure, LLM-free difficulty signal or a parse
  failure — off by default, byte-for-byte the prior behavior when off. See
  [`docs/model-routing.md`](docs/model-routing.md).
- **Agent observability.** Every coordinator skill dispatch is recorded
  (bounded input/output summaries, status, trace id, latency, cost, the memory
  the agent injected); an **Agents** page lets you click an agent and inspect
  what it was thinking. See [`docs/agent-observability.md`](docs/agent-observability.md).
- **LangGraph orchestration on Postgres.** The coordinator and the
  skeptic / discovery / belief-consolidation sweeps are stateful LangGraph
  graphs (conditional routing + `Send` fan-out), checkpointed to Postgres
  (one thread per run). A single pgvector Postgres holds both the `knowledge`
  schema and the operational state (checkpoints + `schedules`).

**Status:** Phases 0–23 complete. The core is field-agnostic; the knowledge
store is consolidated on a single pgvector Postgres; orchestration is
LangGraph; and the latest phase (23) adds end-to-end agent observability.

---

## Quickstart

**Prerequisites:** Docker Desktop running, `uv` installed (`pip install uv`), an Anthropic API key.

```bash
# 1. Clone and configure
git clone https://github.com/bklassen3434/agent-mesh.git
cd agent-mesh
cp .env.example .env
# → Open .env and set ANTHROPIC_API_KEY=sk-ant-...

# 2. Install Python deps (needed for the CLI) and apply the schema + roles
uv sync
uv run mesh.cli init-db             # idempotent: knowledge/agents/runtime/catalog schemas, roles, migrations

# 3. Boot the full stack (10 scouts + worker agents + coordinator + API + wiki + Postgres)
make up
# Wait ~30s for healthchecks to pass

# 4. Run one ingestion cycle (arxiv, HN, GitHub, blogs, leaderboards, …)
make ingest
# Takes 2–5 min — claims are extracted, entities resolved, beliefs synthesized

# 5. Run one falsification sweep (Skeptic challenges the beliefs)
make skeptic

# 6. (optional) Other sweeps
make discover            # autonomous gap-driven investigations
make consolidate-beliefs # semantic belief de-dup + decay/archive

# 7. Open the wiki
open http://localhost:3000
# Also: open http://localhost:8000/status  (operational status page)
#       open http://localhost:8000/docs    (API docs / Swagger)

# 8. Inspect via CLI
uv run mesh.cli pipeline-stats
uv run mesh.cli show-sota-beliefs
uv run mesh.cli investigations list
uv run mesh.cli routing-stats

# 9. Tear down
make down
```

Most knowledge commands and the read API accept `--field <slug>` / `?field=<slug>`
(default `ai-robotics`). The default LLM is Anthropic Haiku 4.5; flip to local
Ollama with `MESH_LLM_PROVIDER=ollama` (see [`docs/llm-setup.md`](docs/llm-setup.md)).

**Want it to run automatically?** Scheduling is built in: a non-blocking
`BackgroundScheduler` reconciles interval/enabled config from a Postgres
`schedules` table on a 30s poll, with a Starlette HTTP control surface. Manage
it from the **Pipelines** page in the wiki, or:

```bash
docker compose --profile scheduler up scheduler -d
```

For Tailscale-only access from your phone: [`docs/deployment.md`](docs/deployment.md).

## What you can click on

Nav: **Daily Brief · Ask · Knowledge ▾ · Graph · Agents · Pipelines** (+ a
`mesh status →` link to the operational status page).

| Surface | What it shows |
|---|---|
| `/` | Home: stat tiles, recent pipeline runs, most-recently-revised beliefs. |
| `/briefing` | Personalized daily digest — Personalizer ranks the last 24h against a markdown profile at `~/.config/agent_mesh/profile.md`. |
| `/ask` | Knowledge chatbot — cited answers grounded in the store, with a coverage badge. |
| `/knowledge/beliefs/[id]` | Headline view. Supporting + contradicting claims with excerpts and source links. **`BeliefSignalsCard`** with the hype↔substance score and individual signals (reproduction count, source diversity, Skeptic attacks). Compact revision timeline. |
| `/knowledge/beliefs/[id]/timeline` | Full revision history with an inline-SVG step-chart of confidence over time. Skeptic challenges colored destructively. |
| `/graph` | Force-directed Cytoscape.js view fed by a pre-aggregated `/api/v1/graph/data` endpoint. Color by entity type, filter chips, click-through to entity pages. |
| `/agents` | Agent roster + the coordinator-star interaction graph. Click an agent → its current memory + recent invocations → drill into one invocation's inputs/outputs/context and a Langfuse deep-link. |
| `/pipelines` | Schedule control: per-job interval + enabled, manual trigger, scheduler status. |
| `/skeptic` | What the Skeptic challenged this week — revisions joined with their trigger counter-claims. |
| `/knowledge/{entities,claims,sources}` | Paginated browsers for the raw provenance graph. |
| `<api>/status` | Operational status page (server-rendered, meta-refresh). Last + next runs, row counts, orchestration state from the checkpoint store, Langfuse 24h trace count. |

Underneath: a typed JSON contract at <http://localhost:8000/docs>. Next.js is
just one consumer — any future client speaks the same JSON.

## Architecture at a glance

```
arxiv  hn  github  bluesky  reddit  blog  leaderboard  web-search  rss  rest-json   ← scouts (connector catalog)
   └────┴─────┴───────┴────────┴──────┴──────┴──────────────┴─────┴──────┘
                                  │  (A2A skill: scout_*, investigate_*)
                                  ▼
                       ┌────────────────────┐
                       │  coordinator       │  ← LangGraph StateGraph, owns all DB writes
                       │  (apps/pipeline)   │     extract → resolve → synthesize
                       └─────────┬──────────┘
                                 │  A2A JSON-RPC (call_skill)
        ┌────────────┬──────────┼───────────┬─────────────┐
        ▼            ▼          ▼           ▼             ▼
   claim-       entity-     sota-       skeptic /     personalizer /
   extractor    tracker     tracker     curator       research-qa
        │
        ▼
 ┌──────────────────────────────────────────────┐
 │  Postgres  (mesh-postgres, pgvector/pg16)     │
 │  knowledge schema: entities · claims ·        │
 │    beliefs · revisions · relationships ·      │
 │    investigations  (+ views, HNSW indexes)    │
 │  agents schema: agent_heuristics · revisions  │
 │    · agent_invocations                        │
 │  runtime schema: pipeline_runs · llm_usage ·  │
 │    processed_items                            │
 │  catalog schema: fields · connectors ·        │
 │    field_connectors                           │
 │  public: LangGraph checkpoints · schedules    │
 │  roles: mesh_writer (coordinator) /           │
 │         mesh_reader (API)                     │
 └──────────────────────────────────────────────┘
        ▲                                  ▲
        │ write                            │ read (mesh_reader)
 ┌─────────────────┐              ┌──────────────────────┐
 │ sweeps (cron):  │              │   api  +  wiki       │
 │ skeptic ·       │              │   /status · /graph · │
 │ discover ·      │              │   /agents · /docs    │
 │ belief-consol.  │              └──────────────────────┘
 └─────────────────┘
```

## Docs

- [`docs/architecture.md`](docs/architecture.md) — system context and phase roadmap
- [`docs/schema.md`](docs/schema.md) — data model and design rationale
- [`docs/postgres-migration.md`](docs/postgres-migration.md) — the DuckDB → Postgres consolidation (Phase 12)
- [`docs/a2a.md`](docs/a2a.md) — A2A wire protocol
- [`docs/agents.md`](docs/agents.md) — agent catalogue
- [`docs/agent-observability.md`](docs/agent-observability.md) — per-agent invocation capture + Agents page (Phase 23)
- [`docs/field-agnostic.md`](docs/field-agnostic.md) — first-class Fields + connector catalog (Phase 17)
- [`docs/entity-resolution.md`](docs/entity-resolution.md) / [`docs/entity-resolution-reconciliation.md`](docs/entity-resolution-reconciliation.md) — semantic entity dedup (Phase 13)
- [`docs/belief-synthesis.md`](docs/belief-synthesis.md) — claim_type-dispatched synthesis + derived confidence (Phase 14)
- [`docs/belief-consolidation.md`](docs/belief-consolidation.md) — append-only belief de-dup + decay/archive (Phase 19)
- [`docs/model-routing.md`](docs/model-routing.md) — tiered cheap/strong routing (Phase 20)
- [`docs/knowledge-chatbot.md`](docs/knowledge-chatbot.md) — the Ask page / research-qa (Phase 21)
- [`docs/autonomous-discovery.md`](docs/autonomous-discovery.md) — gap-driven self-directed investigations (Phase 22)
- [`docs/investigations.md`](docs/investigations.md) — the follow-up loop
- [`docs/derived-signals.md`](docs/derived-signals.md) — belief-quality views + formula
- [`docs/agent-memory.md`](docs/agent-memory.md) / [`docs/episodic-memory.md`](docs/episodic-memory.md) — heuristic + episodic memory
- [`docs/wiki.md`](docs/wiki.md) — why API-in-front-of-Postgres
- [`docs/scheduling.md`](docs/scheduling.md) — the BackgroundScheduler + schedule control
- [`docs/deployment.md`](docs/deployment.md) — Tailscale-only access
- [`docs/personalization.md`](docs/personalization.md) — the briefing profile
- [`docs/development.md`](docs/development.md) — contributor setup
- [`docs/llm-setup.md`](docs/llm-setup.md) — Ollama install + model picks
- [`docs/posts/`](docs/posts/) — long-form write-ups

## What's deferred

- **Self-serve connector onboarding UX (Phase 18).** The connector catalog and
  per-field enablement exist; the no-code onboarding flow to register a new
  field + enable connectors from the wiki is the next step.
- **Skeptic-sweep observability.** Phase 23 captures every coordinator skill
  dispatch automatically; wiring the skeptic sweep's dispatches into the same
  `agent_invocations` capture is a follow-up.
- **Public deployment.** The mesh is local-first with Tailscale-as-auth. A
  public deploy is a separate decision, not a forced part of "production
  readiness."
