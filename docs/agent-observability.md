# Agent observability: inspect what each agent is thinking (Phase 23)

Agent Mesh runs a fleet of A2A agents (claim-extractor, entity-tracker,
sota-tracker, the scouts/connectors, the investigation scouts, and — as later
phases land — the belief-consolidator and discovery agents). The coordinator
dispatches each as a skill call, threading a W3C `traceparent`. Two durable
traces existed before this phase: the `llm_usage` token/cost ledger and the
LangGraph checkpoints. Neither answers the question the user actually asks: **at
a given moment, what was this agent thinking?** — the input it received, the
memory it assembled, the output it produced, the model it used, and how this one
invocation connects to the others in the run.

Phase 23 delivers a durable **agent-invocation record** (coordinator-written,
one row per skill dispatch), a read-only API over it, and a wiki **Agents**
page: click an agent, see its current memory + recent invocations, drill into
one invocation's full inputs/outputs/context, and deep-link to Langfuse for the
raw prompt.

```
 dispatch (coordinator)        capture (best-effort)        surfaces (read-only)
 ─────────────────────        ─────────────────────        ────────────────────
 _dispatch() wraps every  ──▶ AgentInvocation rows     ──▶ /api/v1/agents*  ──┐
 call_skill_node:             accumulate in graph state    (mesh_reader)       │
   input + output (bounded),  → written at finalize                           ▼
   status, trace id, latency, behind the run-exists guard   wiki /agents page:
   model/tokens/cost          (idempotent, never aborts)    graph → agent → memory
   + the agent's optional                                   + recent invocations →
   debug envelope (memory)                                  one invocation's full
                                                            context + Langfuse link
```

## The record

`knowledge.agent_invocations` (migration `014_agent_invocations.sql`) is one row
per coordinator skill dispatch:

| column | meaning |
|---|---|
| `id`, `run_id`, `field_id` | identity; `run_id` groups a run, `field_id` partitions by field |
| `agent`, `skill` | who was dispatched and which skill it served |
| `traceparent`, `trace_id` | the threaded W3C trace; `trace_id` is the Langfuse deep-link key |
| `status`, `error_type`, `error_message` | `ok` \| `error` (mirrors the `TaskError` path) |
| `input_summary`, `output_summary` | **bounded** captures (jsonb) — never unbounded blobs |
| `memory_block`, `applied_heuristic_ids`, `system_prefix_hash` | the memory the agent injected, when it supplies the debug envelope |
| `model`, `latency_ms`, `input_tokens`, `output_tokens`, `cost_usd` | realized model + measured timing + token usage |

`run_id` is a plain indexed column, **not** a hard FK: the `pipeline_runs` row is
only written at finalize, while invocations are recorded as the run unfolds, so
an invocation can briefly exist before its run row does. Indexes:
`(field_id, agent, created_at desc)` (the hot roster / recent-invocations path)
and `(run_id)` (run drill-downs).

`mesh_db.agent_invocations` exposes `create_agent_invocation` (writer) and the
field-scoped readers `list_agent_invocations`, `get_agent_invocation`,
`agent_roster` (per-agent aggregates), and `agent_graph` (the interaction graph).

### Bounded capture, Langfuse for raw content

Stored summaries are capped: the coordinator serializes each input/output and
truncates to `MESH_OBS_CAPTURE_MAX_CHARS` (default 2000), storing
`{truncated, chars, preview}` or `{truncated: false, preview, keys}`. The **raw**
prompt and output are not duplicated into Postgres — they live in Langfuse,
reached by `trace_id`. The one-invocation API returns a Langfuse deep-link
(`{base}/trace/{trace_id}`) when `NEXT_PUBLIC_LANGFUSE_URL` / `LANGFUSE_HOST` is
configured.

## Capture is coordinator-owned and best-effort

- **Coordinator-owned writes.** Invocation rows are written by the coordinator on
  the `mesh_writer` connection — it already holds the input payload, the returned
  output, the traceparent, and the timing. Agents stay write-free per the role
  model; no agent role gains write on this table.
- **Append-only.** The writer has `SELECT, INSERT` and **no `DELETE`** — an audit
  log, matching `claims` / `belief_revisions` / `llm_usage`. There is no
  `update_agent_invocation`.
- **Capture, don't re-architect.** `_dispatch` (in `apps/pipeline/coordinator.py`)
  wraps `call_skill_node`, times it, and builds one `AgentInvocation` from the
  result — it never alters the dispatch's own success/failure. Every coordinator
  dispatch site routes through it (`scout_one`, `extract_one`, entity resolution,
  `update_sota`, the `investigate_*` calls, and investigation re-extraction).
- **Degrade, never block.** Invocations accumulate in graph state (an
  `operator.add` reducer, like `extractions`/`errors`) and are written at
  `finalize` behind the same run-exists guard as the `llm_usage` ledger —
  idempotent on a re-ticked superstep. Each row is written independently; a
  recording failure is logged, never raised. The pipeline's correctness never
  depends on observability.

### Memory capture via an additive debug envelope

The memory block an agent injects (`mesh_agents.memory.build_memory_block`) is
built *inside the agent*, not the coordinator. So memory-using agents attach an
optional, additive **debug envelope** to their skill output under the reserved
`debug` key (`mesh_agents.memory.debug_envelope`): the rendered memory block, the
ids of the heuristics it applied (`build_memory_capture`), the system-prefix hash
it ran under, and the agent's self-reported name. The coordinator folds it into
the row when present; absent, those fields are null and nothing blocks. The
claim-extractor (`extract_claims`) ships the envelope; any agent that adds it
later is captured for free.

## Extensible by construction

Any agent dispatched through the standard skill path is captured with no
per-agent code. The coordinator derives the agent name from a small skill→agent
map (with `scout_*` / `investigate_*` derived by convention), and the debug
envelope's self-reported name overrides it — so future agents
(belief-consolidator, discovery) appear in the view automatically.

> **Scope note.** Phase 23a captures the **coordinator** graph. The skeptic
> sweep (`skeptic_sweep.py`) is a separate orchestrator; its batch path calls the
> LLM directly (no skill dispatch to wrap), so capturing it is a follow-up rather
> than part of this phase. The skeptic/curator agents still appear in the view
> once a sweep is captured through the same record.

## The read API

All `/api/v1/agents*` endpoints run on the `mesh_reader` connection and scope by
`?field=<slug>` (default `ai-robotics`):

- `GET /api/v1/agents` → the roster (`agent_roster`): per-agent invocation count,
  error rate, avg latency, total tokens + cost, last-active + last run.
- `GET /api/v1/agents/{agent}/invocations` → recent invocations, newest first.
- `GET /api/v1/agents/invocations/{id}` → one invocation's full bounded detail,
  its applied heuristic ids resolved to their current text, and a Langfuse link.
- `GET /api/v1/agents/{agent}/memory` → the agent's *current* learned state: its
  active heuristics (`list_heuristics`, active + unexpired) and recent episodic
  history (`recall_history`) — reusing the existing memory readers.
- `GET /api/v1/agents/graph` → a cytoscape-shaped agent-interaction graph.

### The interaction graph

The real call topology is a star: a single `coordinator` hub dispatches every
agent (agents never dispatch each other). `agent_graph` returns a `coordinator`
node plus one node per agent (sized by invocation volume, colored by error rate)
and one `coordinator → agent` edge per agent (width = dispatch volume). Shaped
like `/api/v1/graph/data` so the wiki reuses the cytoscape renderer.

## The wiki Agents page

`/agents` (nav: `Daily Brief | Knowledge ▾ | Graph | Agents | Pipelines`) renders
the agent graph + a roster table. Selecting an agent (graph node or table row)
opens a detail panel with its current memory and recent invocations; expanding an
invocation shows its bounded input/output, the injected memory/context block,
model/latency/tokens/cost, status/error, and a **"View trace in Langfuse"** deep
link — the "what was the agent thinking at this moment" view. Field-scoped via
`?field=`; refresh-on-navigate like the other pages (no realtime transport).

## What this is not

Bounded summaries + a Langfuse link, never full raw prompts/outputs in Postgres.
No new tracing backend or OpenTelemetry collector — it reads/links the existing
plumbing. No agent write access. No live streaming, no editing/replaying agents
from the UI, no cross-field dashboard, no alerting — surfacing data, not paging
on it.
