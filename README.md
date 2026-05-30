# Agent Mesh

A persistent multi-agent system that tracks AI/robotics research through a
network of A2A-protocol agents, with a read-only Next.js wiki on top.

The interesting bits, written up at the end of Phase 7:

- **Falsification-first.** A `Skeptic` agent challenges beliefs that have
  been around long enough or thin enough to deserve it. Counter-claims are
  first-class data with a structured `failure_mode` taxonomy
  (`methodological_flaw`, `cherry_picked_evidence`, …) — not free text the
  next layer has to re-parse. See [`docs/posts/falsification-first.md`](docs/posts/falsification-first.md).
- **Production-ready without deployment.** The mesh runs on cron via
  APScheduler, every dispatch is persisted to Postgres with full lifecycle
  events, and access is single-user Tailscale — the laptop is the
  production environment. See [`docs/posts/production-without-deployment.md`](docs/posts/production-without-deployment.md).
- **A2A wire protocol.** Agents discover each other via JSON-RPC cards;
  the coordinator dispatches by skill_id, never by import. Adding a scout
  is a single Agent Card change.
- **Investigations close the loop.** When Curator sees a stale or thin
  belief it opens an Investigation; on the next pipeline run the
  coordinator dispatches it to scouts with an `investigate_<source>` skill.
  The mesh stops being purely reactive.
- **Derived signals over beliefs.** `belief_hype_substance` combines
  source diversity, cross-source reproduction, Skeptic attack count, and
  severe failure-mode density into a single 0-1 score per belief. Computed
  on read by Postgres views, never stored.

**Status:** Phases 0–7 complete (7c DSPy deferred to a follow-up). Tagged
[`v0.7.0-phase-7`](https://github.com/bklassen3434/agent-mesh/releases).

---

## Quickstart

**Prerequisites:** Docker Desktop running, `uv` installed (`pip install uv`), an Anthropic API key.

```bash
# 1. Clone and configure
git clone https://github.com/bklassen3434/agent-mesh.git
cd agent-mesh
cp .env.example .env
# → Open .env and set ANTHROPIC_API_KEY=sk-ant-...

# 2. Install Python deps (needed for the CLI)
uv sync

# 3. Boot the stack (13 containers: 7 scouts + 4 worker agents + API + wiki)
make up
# Wait ~30s for all healthchecks to pass

# 4. Run one ingestion cycle (fetches from arxiv, HN, GitHub, blogs, etc.)
make pipeline
# Takes 2–5 min — you'll see logs as claims are extracted

# 5. Run one falsification sweep (Skeptic challenges the beliefs)
make skeptic
# Takes ~1 min

# 6. Open the wiki
open http://localhost:3000
# Also: open http://localhost:8000/status  (operational status page)
#       open http://localhost:8000/docs    (API docs)

# 7. Inspect via CLI
uv run mesh.cli pipeline-stats
uv run mesh.cli show-sota-beliefs
uv run mesh.cli investigations list

# 8. Tear down
make down
```

**Want it to run automatically?** Start the scheduler (runs pipeline every 6h, sweep daily):
```bash
docker compose --profile scheduler up scheduler -d
uv run mesh.cli schedule status   # check next fire times
```

## Quick start

```bash
git clone https://github.com/bklassen3434/agent-mesh.git
cd agent-mesh
uv sync
cp .env.example .env                # set ANTHROPIC_API_KEY (default LLM)

# Bring up the full stack: 7 scouts + 5 worker agents + read API + wiki.
make up
make wiki                           # opens http://localhost:3000
make api                            # opens http://localhost:8000/docs

# One ingestion cycle. Default LLM is Anthropic Haiku 4.5; flip to local
# Ollama with MESH_LLM_PROVIDER=ollama.
make pipeline

# One falsification sweep — Curator picks, Skeptic challenges,
# counter-claims + revisions land in the DB.
make skeptic

# Schedule both jobs on cron (every 6h pipeline, daily 03:00 sweep):
docker compose --profile scheduler up scheduler -d

# Inspect via CLI.
uv run mesh.cli pipeline-stats
uv run mesh.cli show-sota-beliefs
uv run mesh.cli investigations list
uv run mesh.cli schedule status
```

For Tailscale-only access from your phone:
[`docs/deployment.md`](docs/deployment.md).

## What you can click on

After a populated `make pipeline && make skeptic`:

| Surface | What it shows |
|---|---|
| `/` | Home: stat tiles, recent pipeline runs, most-recently-revised beliefs. |
| `/briefing` | Personalized daily digest — Personalizer ranks the last 24h against a markdown profile at `~/.config/agent_mesh/profile.md`. |
| `/beliefs/[id]` | Headline view. Supporting + contradicting claims with excerpts and source links. **`BeliefSignalsCard`** with the hype↔substance score, individual signals (reproduction count, source diversity, Skeptic attacks), and the anchor explanation. Compact revision timeline. |
| `/beliefs/[id]/timeline` | Full revision history with an inline-SVG step-chart of confidence over time. Skeptic challenges colored destructively. |
| `/graph` | Cytoscape.js view of all entities + relationships. Color by entity type, filter chips, click-through to entity pages. |
| `/skeptic` | What the Skeptic challenged this week — revisions joined with their trigger counter-claims. |
| `/entities`, `/claims`, `/sources` | Paginated browsers for the raw provenance graph. |
| `<api>/status` | Operational status page (server-rendered HTML, meta-refresh 60s). Last + next runs, row counts, recent task failures, Langfuse 24h trace count. Linked from the nav as "mesh status →". |

Underneath: typed JSON contract at <http://localhost:8000/docs>.
Next.js is just one consumer — any future client speaks the same JSON.

## Architecture at a glance

```
arxiv  hn    github  bluesky  reddit  blog  leaderboard          ← scouts (7)
   │     │     │       │        │      │      │
   └─────┴─────┴───────┴────────┴──────┴──────┘
                       │
                       ▼  (A2A skill: scout_*, investigate_*)
              ┌────────────────┐
              │  coordinator   │  ← dispatches by skill_id, owns all DB writes
              └────┬───────┬───┘
                   │       │
                   ▼       ▼
        ┌──────────────┐  ┌──────────────────┐
        │ claim_extr.  │  │ entity_tracker   │
        │ sota_tracker │  │ (LLM-resolved)   │
        └──────────────┘  └──────────────────┘
                   │
                   ▼
         ┌─────────────────┐
         │  Postgres       │
         │  (writer role)  │
         │  - entities     │
         │  - claims       │
         │  - beliefs      │
         │  - belief_rev.  │
         │  - investig.    │
         │  - agent_tasks  │
         │  + views        │
         └─────────────────┘
                   ▲
                   │
   ┌───────────────┴────────────────┐
   │                                │
   ▼                                ▼
┌──────────────┐         ┌──────────────────┐
│ skeptic-sweep│         │  api  +  wiki    │
│ (cron)       │         │  /status + /graph│
│  Curator →   │         └──────────────────┘
│  Skeptic →   │
│  revisions   │
└──────────────┘
```

## Docs

- [`docs/architecture.md`](docs/architecture.md) — system context and phase roadmap
- [`docs/schema.md`](docs/schema.md) — data model and design rationale
- [`docs/a2a.md`](docs/a2a.md) — wire protocol + Phase 6b orchestrator-side durability
- [`docs/agents.md`](docs/agents.md) — agent catalogue
- [`docs/wiki.md`](docs/wiki.md) — why API-in-front-of-Postgres
- [`docs/scheduling.md`](docs/scheduling.md) — Phase 6a cron
- [`docs/deployment.md`](docs/deployment.md) — Tailscale-only access
- [`docs/personalization.md`](docs/personalization.md) — Phase 5c briefing profile
- [`docs/investigations.md`](docs/investigations.md) — Phase 7a follow-up loop
- [`docs/derived-signals.md`](docs/derived-signals.md) — Phase 7b views + formula
- [`docs/development.md`](docs/development.md) — contributor setup
- [`docs/llm-setup.md`](docs/llm-setup.md) — Ollama install + model picks
- [`docs/posts/`](docs/posts/) — long-form write-ups

## What's deferred

- **DSPy optimization** for the LLM-using agents (claim_extractor, skeptic,
  curator, personalizer) — wants a populated DB worth of training signal.
  Plumbing tagged for a follow-up sub-phase.
- **Investigation depth** for non-arxiv scouts. arxiv runs a real
  hypothesis-directed search; the other six advertise the
  `investigate_<source>` skill but return empty. Filling each in is per-source
  work.
- **Public deployment.** Phase 6 stopped at Tailscale-as-auth. Public deploy
  is a separate decision, not a forced part of "production readiness."
