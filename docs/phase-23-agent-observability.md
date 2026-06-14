# Phase 23 — Agent Observability: Inspect What Each Agent Is Thinking

## Context

Agent Mesh runs a fleet of A2A agents (claim-extractor, skeptic, curator,
personalizer, the scouts/connectors, and — as later phases land — the
belief-consolidator and discovery agents). The coordinator dispatches each as a
skill call (`mesh_a2a.node.call_skill_node` /
`MeshA2AClient.call_skill_blocking`), threading a W3C `traceparent`, and records
two durable traces today:

- **`llm_usage`** — per skill/agent token + cost ledger, written at
  coordinator/sweep finalize.
- **LangGraph checkpoints** — per-run state in Postgres, surfaced by
  `mesh_a2a.checkpoint.read_run_states` on the `/status` page.

Plus optional **Langfuse** spans (`mesh_tracing.trace_generation`) when
configured, and **episodic recall** (`mesh_db.episodic.recall_history`) which
*reconstructs* an agent's action history from its claim/revision artifacts.

What no surface exposes is the question the user is actually asking: **at a given
moment, what was this agent thinking?** — the exact input it received, the
context + memory block it assembled (its applied heuristics and episodic recall,
via `mesh_agents.memory.build_memory_block`), the output it produced, the model
it used, and how that one invocation connects to the others in the run. That
information is either ephemeral (only in Langfuse, if enabled) or scattered across
tables that require manual joins. There is **no per-invocation record** and **no
agent-centric view**.

This phase delivers both: a durable **agent-invocation record** (coordinator-
written, one row per skill call, capturing the inputs/outputs/context/memory/
trace), a read API over it, and an **agent graph** wiki page — click an agent
node, drill into its recent invocations, and inspect one invocation's full
inputs, outputs, applied memory/heuristics, and cost, with a deep link to the
Langfuse trace for the raw prompt. It reuses the cytoscape graph view
(`/graph`) and the run-detail drill-down (`/pipelines`) as visual precedent.

This phase is **read-mostly**: the only write is the coordinator persisting
invocation rows as it dispatches (respecting coordinator-owned writes). It is
**designed extensibly** — as new agents land (belief-consolidator from Phase 19,
discovery from Phase 22), they appear in the view automatically because they go
through the same dispatch path; nothing here depends on those phases.

Read before writing any code — do not guess table, column, function, or route
details:

- The dispatch path: `mesh_a2a.node.call_skill_node` (and `TaskError`),
  `MeshA2AClient.call_skill_blocking` / `submit_task`, and how the coordinator
  calls skills with `traceparent` (`apps/pipeline/coordinator.py` — `scout_one`,
  `extract_one`, `track_entities`, `synthesize`, `dispatch_investigations`).
- The trace plumbing: `mesh_a2a.tracing` (`new_traceparent`, `extract_trace_id`,
  `TRACEPARENT_KEY`), `mesh_tracing.trace_generation`, and the `llm_usage` ledger
  (`mesh_db.llm_usage` — `create_llm_usage`, its columns: run_id, skill_id,
  agent_name, tokens, cost_usd, model).
- The memory subsystem: `mesh_agents.memory.build_memory_block` /
  `recall_block` / `format_heuristic_block` (what an agent actually injects), and
  `mesh_db.episodic.recall_history` / `EpisodicEntry` / `EpisodicOutcome`,
  `mesh_db.heuristics.list_applicable_heuristics` / `AgentHeuristic`.
- The checkpoint surface: `mesh_a2a.checkpoint.read_run_states`,
  `RunCheckpointState`, `thread_config` (thread_id == run_id).
- Field scoping (Phase 17): `field_id` on `pipeline_runs` / knowledge tables, the
  `?field=` API param, `load_profile`.
- The wiki precedents: the `/graph` cytoscape view
  (`apps/wiki/src/components/graph-view.tsx`, fed by `/api/v1/graph/data` ←
  `mesh_db.graph`), the `/pipelines` run-detail drill-down
  (`pipelines-panel.tsx`, the expandable `RunDetail`), the nav, shadcn
  primitives, `make types`, and the Playwright setup.
- The Postgres roles + grants (`005_grants.sql` / `006_entity_resolution.sql`) —
  writer insert/update, reader select, no DELETE.

---

## Goal

A durable, field-scoped **agent-invocation** record written by the coordinator on
every skill dispatch (input + output summaries, the applied memory/heuristic
block, model, latency, tokens, status, trace id), a read-only API over it
(`/api/v1/agents*`), and a wiki **Agents** page: an agent-interaction graph where
clicking an agent reveals its recent invocations, its applied memory + episodic
history, its cost/error profile, and a drill-down into a single invocation's full
inputs/outputs/context — with a deep link to Langfuse for the raw prompt. No
engine change; coordinator-owned writes only; reuses existing graph + run-detail
patterns.

---

## Principles (do not violate)

- **Coordinator-owned writes; no agent role gains write.** Invocation rows are
  written by the coordinator (which already holds the input payload, the returned
  output, the traceparent, and the timing) on the `mesh_writer` connection —
  **not** by the agents. Agents stay write-free per the role model.
- **Capture, don't re-architect.** Observability is a passive recorder. It must
  not change what agents do, how the coordinator routes, synthesis, confidence,
  or any belief/claim write. If capturing a field would require an engine change,
  stop and report.
- **Append-only; never delete.** Invocation rows are an audit log: insert-only,
  no UPDATE-to-erase, no DELETE grant. Match the append-only posture of revisions
  and `llm_usage`.
- **Field-scoped + bounded.** Every invocation carries `field_id`; every read
  filters by it. Stored input/output are **summaries/bounded captures** (capped
  size, never unbounded blobs) — large content lives in Langfuse, referenced by
  trace id, not duplicated wholesale into Postgres.
- **Read-only, role-respecting API.** Every `/api/v1/agents*` endpoint runs on the
  `mesh_reader` connection. The wiki view is pure read.
- **Degrade, never block.** Recording an invocation must never fail or slow a
  pipeline run: capture is best-effort (a recording error records into
  `state["errors"]` and the run continues, exactly like `call_skill_node`'s own
  failure posture). The pipeline's correctness never depends on observability.
- **Extensible by construction.** Any agent dispatched through the standard skill
  path is captured and appears in the view with no per-agent code — so future
  agents (belief-consolidator, discovery) show up for free.

---

## Scope

### 1. Agent-invocation record — block 23a

The durable per-call capture, coordinator-written.

- Migration `014_agent_invocations.sql` (014 is the next free number after Phase
  22's `013`; coordinate via the roadmap if numbering shifts):
  `agents.agent_invocations(id text pk, run_id text not null, field_id text
  not null references catalog.fields(id), agent text not null, skill text not
  null, traceparent text, trace_id text, status text not null, error_type text,
  error_message text, input_summary jsonb, output_summary jsonb, memory_block
  text, applied_heuristic_ids text[], system_prefix_hash text, model text,
  latency_ms integer, input_tokens integer, output_tokens integer, cost_usd
  double precision, created_at timestamptz not null default now())`. Index on
  `(field_id, agent, created_at desc)` and `(run_id)`. `run_id` is a plain
  indexed column (the `pipeline_runs` row is only written at finalize, so no hard
  FK — document this). Grants: writer insert/select, reader select. **No DELETE.**
- `packages/mesh-db/src/mesh_db/agent_invocations.py`:
  `create_agent_invocation(conn, model)` (writer), and reader helpers
  `list_agent_invocations(conn, *, field_id, agent=None, run_id=None, limit)`,
  `get_agent_invocation(conn, id)`, and `agent_roster(conn, *, field_id)` —
  per-agent aggregates (invocation count, error rate, avg latency, total tokens +
  `cost_usd`, last_active). `AgentInvocation` Pydantic model in `mesh_models`.
- **Capture at dispatch.** Wrap the coordinator's skill-call sites so that after
  each `call_skill_node` / `call_skill_blocking` it records an
  `AgentInvocation`: the input payload (bounded summary), the output (bounded
  summary), status/error from the `TaskError` path, `traceparent` →
  `trace_id`, latency, and — joined or carried from the LLM result — model +
  tokens + cost. **Memory capture:** the rendered memory block + applied
  heuristic ids + system-prefix hash are built *inside* the agent
  (`build_memory_block`); have the agent return them in an **optional debug
  envelope** on its skill output (additive, ignorable), which the coordinator
  folds into the row. When absent, the field is null — never block on it.

**Exit:** migration applies; every coordinator skill dispatch records an
`AgentInvocation` (status, input/output summaries, trace id, latency, model/
tokens when present, memory block when the agent supplies it); recording is
best-effort and never aborts a run; reads are field-scoped; unit-tested against
the testcontainer DB; `ruff` + `mypy --strict` clean. Tag `v0.23.0-phase-23a`.

### 2. Agent read API — block 23b

Expose the roster, per-agent invocations, one invocation, and the interaction
graph.

- `apps/api` router (`mesh_reader` connection, `?field=` scoping, mirror existing
  routers):
  - `GET /api/v1/agents?field=<slug>` → roster: each agent with its aggregate
    stats (`agent_roster`) + last-active + current/last run linkage.
  - `GET /api/v1/agents/{agent}/invocations?field=<slug>&limit=` → recent
    invocations (summaries).
  - `GET /api/v1/agents/invocations/{id}` → one invocation: full bounded
    input/output, memory block, applied heuristic ids (resolved to heuristic
    text), trace id + a Langfuse deep-link (when `NEXT_PUBLIC_LANGFUSE_URL` /
    `LANGFUSE_HOST` configured), model/tokens/cost.
  - `GET /api/v1/agents/{agent}/memory?field=<slug>` → the agent's *current*
    learned state: its active heuristics (`list_applicable_heuristics`) + recent
    episodic history (`recall_history`) — "what this agent knows now," reusing the
    existing memory readers.
  - `GET /api/v1/agents/graph?field=<slug>` → an **agent-interaction graph**:
    nodes = agents, edges = who-dispatches-whom with call volumes, aggregated
    from `agent_invocations` (+ the coordinator's known call topology). Shape it
    like `/api/v1/graph/data` so the wiki can reuse the cytoscape renderer.
- Regenerate `make types`; CI drift check stays green.

**Exit:** all `/api/v1/agents*` endpoints return field-scoped data; the roster
aggregates correctly; one-invocation returns full context incl. memory + a
Langfuse link when configured; the agent-graph endpoint returns a
cytoscape-shaped payload; `make types` clean, no drift; `ruff` + `mypy --strict`
clean. Tag `v0.23.0-phase-23b`.

### 3. Wiki Agents page — block 23c

The interactive observability surface.

- A new **Agents** nav entry (mirror the existing nav; respect Phase 18's field
  switcher if present, else carry a `?field=` selector). The page renders the
  **agent-interaction graph** by reusing the cytoscape component pattern from
  `graph-view.tsx` (nodes = agents sized by invocation volume / colored by error
  rate; edges = dispatch volume).
- Clicking an agent node opens a side panel (reuse the `/pipelines` `RunDetail`
  drill-down idiom): the agent's roster stats, its **current memory** (active
  heuristics + recent episodic history with outcome labels), and a list of its
  **recent invocations**.
- Clicking an invocation expands its full detail: input summary, output summary,
  the **applied memory/context block** it injected, model + latency + tokens +
  cost, status/error, and a **"View trace in Langfuse"** deep link when
  configured. This is the "what was the agent thinking at this moment" view.
- shadcn primitives only; loading/empty/error states like the other pages.
- Playwright: a page object + spec covering graph render, agent-node click →
  panel, invocation drill-down → context/memory visible, field scoping. Extend
  the mock server with `/api/v1/agents*` fixtures.

**Exit:** the wiki **Agents** page renders the agent graph; clicking an agent
shows its memory + recent invocations; drilling into an invocation shows its
inputs/outputs/context/memory + a Langfuse link; all field-scoped; Playwright
covers it; wiki lint/typecheck/build + `ruff` + `mypy --strict` clean. Tag
`v0.23.0-phase-23c`.

### 4. Docs — block 23d

Add `docs/agent-observability.md`: the invocation record + what it captures (and
the bounded-capture / Langfuse-for-raw-content split), the coordinator-owned
write posture, the agent-graph + drill-down model, the memory-inspection reuse of
the episodic/heuristic readers, and field scoping. Match `docs/agent-memory.md` /
`docs/a2a.md` style. Update `CLAUDE.md`'s phase-status paragraph + env-var table
(any capture-size knob, e.g. `MESH_OBS_CAPTURE_MAX_CHARS`).

---

## Out of Scope (do not build)

- **Persisting full raw prompts/outputs in Postgres.** Bounded summaries + a
  Langfuse trace link only; raw content lives in Langfuse.
- **A new tracing backend, OpenTelemetry export, or replacing Langfuse.** This
  phase reads/links the existing trace plumbing; it doesn't add a collector.
- **Agent write access / agent-side DB logging.** Capture is coordinator-written.
- **Live streaming / websockets of agent state.** Periodic refresh like
  `/pipelines` (30s) — no realtime transport.
- **Editing/replaying/intervening on agents from the UI** (re-running a skill,
  hot-editing memory). Read-only observability only.
- **Cross-field agent comparison or a global (all-field) dashboard.** One field
  per view.
- **Per-agent alerting / anomaly detection.** Surfacing data, not paging on it.

---

## Exit Criteria

- [ ] Migration `014` adds `agent_invocations` (field-scoped, indexed,
      append-only, **no DELETE**); writer insert/select, reader select
- [ ] The coordinator records an `AgentInvocation` per skill dispatch
      (input/output summaries, status, trace id, latency, model/tokens/cost,
      memory block when the agent supplies the debug envelope); capture is
      best-effort and never aborts a run
- [ ] `/api/v1/agents`, `/agents/{agent}/invocations`,
      `/agents/invocations/{id}`, `/agents/{agent}/memory`, `/agents/graph` exist,
      are read-only + field-scoped; `make types` clean, no drift
- [ ] Wiki **Agents** page: agent graph → click agent → memory + recent
      invocations → drill into one invocation's inputs/outputs/context/memory +
      Langfuse link; Playwright covers it
- [ ] New agents (any dispatched through the standard skill path) appear in the
      view with no per-agent code
- [ ] `docs/agent-observability.md` added; `CLAUDE.md` phase status + env table
      updated
- [ ] `ruff` + `mypy --strict` clean across touched packages; existing pytest +
      Playwright unaffected
- [ ] No engine change; coordinator-owned writes only; field isolation preserved;
      no role relaxation; append-only (no DELETE)

---

## Commit Convention

One logical commit per unit; conventional messages:

```
feat(db,models): add agent_invocations record + readers (migration 014)
feat(coordinator): record AgentInvocation per skill dispatch (best-effort)
feat(api): add /api/v1/agents* observability endpoints
feat(wiki): add Agents page (agent graph + invocation/memory drill-down)
docs: add agent-observability.md; update CLAUDE.md
```

Tags map to blocks: `v0.23.0-phase-23a` (invocation record + capture), `…-23b`
(read API), `…-23c` (wiki Agents page), `…-23d` (docs). Execute 23a → 23c in
order — the record is the foundation the API + UI read. Lint, types, and a clean
pipeline run (with capture on) are the bar before each tag. Report any principle
conflict (e.g. capturing the memory block would require an agent write) before
working around it.
