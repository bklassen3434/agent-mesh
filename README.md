# Agent Mesh

A persistent, **field-agnostic** multi-agent system that watches a research
field, builds a living knowledge base from it, and serves it through a Next.js
wiki. It ships seeded for AI/robotics, but the core never branches on the
field — point it at a new one and the same loop applies.

The knowledge base is built from two record types:

- **Claims** — immutable facts extracted from a source ("model X scores 92 on benchmark Y").
- **Beliefs** — mutable syntheses over many claims, with a confidence **derived** from
  evidence (source diversity, reproduction, skeptic attacks), never hardcoded.

A single **controller** senses the store, decides what to do, and does it — with no
scheduler and no human in the loop.

**Status:** Phases 0–24 complete; live end-to-end on a Raspberry Pi. See
[`docs/history.md`](docs/history.md) for how it got here (decisions, dead-ends, bugs).

---

## Architecture at a glance

```
  arxiv · HN · GitHub · Bluesky · Reddit · blogs/RSS · leaderboards · web search
        │                    (a per-field connector catalog, enabled per field)
        ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  CONTROLLER  (mesh-controller — the sole orchestrator + only writer)│
  │                                                                    │
  │   sense ──► the field becomes a list of Tensions (what's unsettled)│
  │   plan  ──► a rule engine maps each tension to a skill             │
  │   act   ──► dispatch skills; apply their effects through a gateway │
  │            ┌──────────────────────────────────────────────┐       │
  │            │  scout → extract → resolve → synthesize →     │       │
  │            │  challenge → consolidate → investigate        │       │
  │            └──────────────────────────────────────────────┘       │
  │   loop to quiescence, then idle-sleep and re-sense (--forever)     │
  └───────────────────────────────┬────────────────────────────────────┘
                                   │ writes (mesh_writer role)
                                   ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  POSTGRES  (single pgvector/pg16 instance, split by concern)       │
  │   knowledge · entities, claims, beliefs, revisions, relationships, │
  │              investigations                                        │
  │   agents    · agent memory (heuristics) + invocation observability │
  │   runtime   · pipeline_runs, llm_usage ledger, processed_items     │
  │   catalog   · fields, connectors, per-field enablement             │
  └───────────────────────────────┬────────────────────────────────────┘
                                   │ reads (mesh_reader role)
                                   ▼
  ┌──────────────────┐        ┌────────────────────────────────────────┐
  │  API  (:8000)    │◄───────│  WIKI  (:3000, Next.js)                 │
  │  read-only JSON  │        │  chatbot · brief · graph · agents ·     │
  │  /docs Swagger   │        │  knowledge browser · fields/connectors  │
  └──────────────────┘        └────────────────────────────────────────┘
```

The controller is a plain async loop, not a graph. Everything — cadence, retries,
escalation — comes from the rule engine's own cooldowns plus an idle backoff. LLM
**skills** (prompt + model + structured output + injected memory) are the agentic unit.
Deeper: [`docs/architecture.md`](docs/architecture.md), [`docs/deterministic-controller.md`](docs/deterministic-controller.md).

---

## Quickstart

**Prerequisites:** Docker Desktop, `uv` (`pip install uv`), an Anthropic API key.

```bash
git clone https://github.com/bklassen3434/agent-mesh.git
cd agent-mesh
cp .env.example .env            # set ANTHROPIC_API_KEY=sk-ant-...
uv sync
uv run mesh.cli init-db         # idempotent: schemas + roles + migrations

make up                         # postgres + controller + api + wiki (+ helpers)
open http://localhost:3000      # the wiki (front page is the chatbot)
open http://localhost:8000/docs # API / Swagger
```

`make up` starts the controller in `--forever` mode — it runs the whole loop, idles
between empty passes, and never needs a cron. To drive it by hand instead:

```bash
uv run mesh-controller                 # shadow: preview one round's plan, write nothing
uv run mesh-controller --apply         # act, looping to quiescence
make controller                        # shadow, in Docker
make controller-apply                  # apply, in Docker
```

Inspect what it did:

```bash
uv run mesh.cli pipeline-stats
uv run mesh.cli show-sota-beliefs
uv run mesh.cli investigations list
uv run mesh.cli routing-stats          # per-tier LLM cost
```

Most CLI/API surfaces take `--field <slug>` / `?field=<slug>` (default `ai-robotics`).
Default LLM is Anthropic Haiku 4.5; flip to local Ollama with `MESH_LLM_PROVIDER=ollama`
or a Groq open-weight cheap tier — see [`docs/llm-setup.md`](docs/llm-setup.md) and
[`docs/model-routing.md`](docs/model-routing.md).

---

## What you can click on

The front page (`/`) is a grounded chatbot. Nav (admin view):
**Daily Brief · Knowledge ▾ · Graph · Agents · Fields · Connectors**.

| Surface | What it shows |
|---|---|
| `/` | Chatbot — cited answers grounded in the store, with a coverage badge. |
| `/briefing` | Personalized daily digest of the last 24h, ranked against a markdown profile. |
| `/knowledge/beliefs/[id]` | A belief with its supporting + contradicting claims, a hype↔substance signal card, and a confidence-over-time timeline. |
| `/graph` | Force-directed entity graph, fed by a pre-aggregated endpoint. |
| `/agents` | Agent roster + interaction graph. Click an agent → its memory and recent invocations → one invocation's inputs/outputs/cost. |
| `/fields`, `/connectors` | Register a field, enable/configure its sources (admin). |
| `/knowledge/{entities,claims,sources}` | Paginated browsers for the raw provenance graph. |

Everything is a consumer of one typed JSON contract (`/docs`); the wiki is just the
first client. **Admin vs. beta** is a property of the running wiki instance
(`MESH_ADMIN_MODE`), not the browser — a public visitor gets an anonymous, view-only,
rate-limited chatbot and nothing else.

---

## Development

```bash
uv run pytest                 # tests use an ephemeral pgvector container (no LLM, mocked)
uv run ruff check . && uv run mypy .
make check                    # full local CI mirror: ruff + mypy + pytest + wiki lint/build + E2E
make hooks                    # one-time: pre-push guard against API↔wiki drift
```

See [`CLAUDE.md`](CLAUDE.md) for the full architecture rundown, invariants, and the
complete environment-variable table, and [`docs/development.md`](docs/development.md)
for contributor setup.

---

## Docs

**Start here**
- [`docs/history.md`](docs/history.md) — how the project was built: the pivots, dead-ends, and bugs
- [`docs/architecture.md`](docs/architecture.md) — system context + the controller loop
- [`docs/deterministic-controller.md`](docs/deterministic-controller.md) — the rule engine, tensions, skills, effects
- [`docs/schema.md`](docs/schema.md) — the data model and its invariants

**Subsystems**
- [`docs/field-agnostic.md`](docs/field-agnostic.md) — first-class Fields + connector catalog
- [`docs/entity-resolution.md`](docs/entity-resolution.md) — semantic entity de-dup (block → match → merge)
- [`docs/belief-synthesis.md`](docs/belief-synthesis.md) · [`docs/derived-signals.md`](docs/derived-signals.md) — typed synthesis + derived confidence
- [`docs/belief-consolidation.md`](docs/belief-consolidation.md) — append-only belief de-dup + decay/archive
- [`docs/model-routing.md`](docs/model-routing.md) — tiered cheap/strong LLM routing
- [`docs/autonomous-discovery.md`](docs/autonomous-discovery.md) · [`docs/investigations.md`](docs/investigations.md) — self-directed gap-filling
- [`docs/agent-memory.md`](docs/agent-memory.md) · [`docs/episodic-memory.md`](docs/episodic-memory.md) — how agents learn
- [`docs/agent-observability.md`](docs/agent-observability.md) — per-dispatch invocation capture + Agents page
- [`docs/knowledge-chatbot.md`](docs/knowledge-chatbot.md) — the grounded Ask page

**Infrastructure & ops**
- [`docs/postgres-migration.md`](docs/postgres-migration.md) — the DuckDB → single-Postgres consolidation
- [`docs/a2a.md`](docs/a2a.md) — the (now-orphaned) A2A wire protocol
- [`docs/cost-baseline.md`](docs/cost-baseline.md) — where the money goes and how it was cut
- [`docs/raspberry-pi.md`](docs/raspberry-pi.md) · [`docs/deployment.md`](docs/deployment.md) — always-on Pi deploy + Tailscale access
- [`docs/telegram-bridge.md`](docs/telegram-bridge.md) — chat + daily brief over Telegram
- [`docs/wiki.md`](docs/wiki.md) — why an API sits in front of Postgres
- [`docs/posts/`](docs/posts/) — long-form write-ups
