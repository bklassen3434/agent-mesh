# A2A Protocol Layer

This document explains how Agent Mesh uses the [Agent2Agent (A2A) protocol](https://a2aproject.github.io/A2A/) to turn local Python classes into distributed HTTP agents, and how Phase 5a moves invocation from sync request/response to task-based submit-then-poll.

---

## Why A2A?

Phase 1 ran all four agents in the same process. That works but limits horizontal scaling and prevents independent deployment. A2A gives each agent a standardized HTTP interface so any compatible client can discover and call it without knowing its implementation language or framework.

---

## Key architectural decisions

### Coordinator owns all DB writes

The coordinator is the only process that touches DuckDB. Agents are pure functions: they receive typed JSON input and return typed JSON output with no side effects.

**Why:** DuckDB supports only one writer at a time. Routing all writes through the coordinator keeps the constraint trivially satisfied and makes the coordinator the single source of truth.

**How it works in practice:**
- Before calling `resolve_entities`, the coordinator fetches all existing entities from DB and passes them in the request.
- Before calling `update_sota`, the coordinator fetches existing SOTA beliefs and passes them.
- After each skill call, the coordinator persists the results (new entities, claims, belief revisions).

### Real capability discovery (no hardcoded routing)

The coordinator never imports agent classes. On startup it fetches `/.well-known/agent-card.json` from each configured base URL and builds a `skill_id → agent_url` map. Dispatch logic only ever calls `call_skill_blocking(skill_id, payload)`.

**Why:** This is the point of A2A. If you hardcode URLs in dispatch logic you've just done HTTP-based in-process calls with extra steps. Real discovery lets you swap agents, add new implementations, or route to remote agents without touching the coordinator.

### Task-based async (Phase 5a)

Phase 5a moves the wire protocol from sync `send_message` (which blocked until the agent finished) to a submit-then-poll task pattern. The client submits a task, immediately gets back a `task_id`, and polls `GET /mesh/tasks/{task_id}` until the agent reports `completed` or `failed`.

**Why:** As the mesh grows (Phase 5b adds four more scouts, 5c adds a Personalizer), keeping the dispatch sync means the coordinator's request-side timeouts get coupled to the slowest agent in every fan-out. Submit-then-poll lets the orchestrator stage work, sleep, and resume — fan-outs no longer wedge a single request socket per skill.

`MeshA2AClient.call_skill_blocking()` preserves the appearance of a sync call at the call site: it submits, polls, and returns the result dict. This is what every orchestrator uses. No coordinator code path opens an `httpx.post` to an agent directly anymore.

### Polling, not push

A2A supports both polling (`tasks/get`) and push notifications. The mesh uses polling-only in Phase 5a. Push notifications are a possible Phase 6 addition if polling intervals become a problem; they aren't now.

### Task state is in-memory on the agent, durable on the orchestrator

Each agent process holds an in-memory `TaskRegistry` (`packages/mesh-a2a/src/mesh_a2a/task_registry.py`): a `dict[task_id, TaskRecord]` protected by an `asyncio.Lock`. Records carry status (`pending | running | completed | failed`), result dict, error string, and timestamps. Agent-side state is still ephemeral — if an agent process restarts mid-task, that task is lost on the agent side.

**Orchestrator-side, every dispatch is now durable (Phase 6b).** `call_skill_blocking` accepts an optional `task_recorder: TaskRecorder` and drives it through the dispatch lifecycle: `record_pending` after submit, `record_running` on the first non-pending status, `record_heartbeat` every N polls, and `record_completed` / `record_failed` on terminal states. The coordinator and skeptic-sweep construct a `DuckDBTaskRecorder` bound to the current run id and pass it in, so every skill call leaves a trail in the `agent_tasks` + `agent_task_events` tables. The status page reads those tables.

If the orchestrator crashes mid-run, the recovery story is "fail visibly, don't try to resume." On startup, the coordinator and sweep call `sweep_orphaned_tasks(threshold_seconds=MESH_TASK_RESUME_THRESHOLD)` (default 600s) which marks any pending/running tasks whose `updated_at` is older than the threshold as `failed` with `error='orphaned_on_restart'`. No retry, no resumption — the operator can re-run the pipeline manually and the status page surfaces the orphans in the "recent failures" panel.

### No auth

Agent cards declare no security schemes. The coordinator does not send credentials. The network is trusted (docker-compose internal network). Phase 6 will add Bearer token or mTLS.

### W3C traceparent for distributed tracing

The coordinator generates a `traceparent` value for each pipeline run (format: `00-{trace_id}-{parent_id}-01`). Every `submit_task` body carries this so an agent's Langfuse generations can attach to the same trace ID, producing a single trace tree per pipeline run.

---

## Protocol wire format

### Agent Card

Every agent exposes its card at `GET /.well-known/agent-card.json`:

```json
{
  "name": "Entity Tracker",
  "description": "Resolves candidate entity names against known entities.",
  "version": "0.1.0",
  "defaultInputModes": ["application/json"],
  "defaultOutputModes": ["application/json"],
  "capabilities": { "streaming": false, "pushNotifications": false },
  "supportedInterfaces": [
    { "protocolBinding": "JSONRPC", "url": "http://entity-tracker:8003", "protocolVersion": "1.0" }
  ],
  "skills": [
    { "id": "resolve_entities", "name": "Resolve Entities", "description": "..." }
  ]
}
```

The agent card is used solely for **discovery** (skill-id → base URL). Skill invocation goes through the mesh task endpoints below, not the SDK's JSON-RPC client.

### Task submit

```http
POST /mesh/tasks/submit HTTP/1.1
Content-Type: application/json

{
  "skill_id": "resolve_entities",
  "payload": { "candidate_names": ["GPT-4"], "existing_entities": [] },
  "traceparent": "00-abc123...-def456...-01"
}
```

Response: `202 Accepted`, body `{ "task_id": "uuid-v4" }`. The server kicks the work into an `asyncio.create_task` and returns immediately.

### Task poll

```http
GET /mesh/tasks/{task_id} HTTP/1.1
```

Response:

```json
{
  "task_id": "abc-uuid",
  "skill_id": "resolve_entities",
  "status": "completed",
  "result": { "resolved": [{ "name": "GPT-4", "entity_id": "...", "is_new": true }] },
  "error": null,
  "created_at": "2026-05-25T20:00:00+00:00",
  "started_at": "2026-05-25T20:00:00.005000+00:00",
  "finished_at": "2026-05-25T20:00:02.450000+00:00"
}
```

Possible `status` values: `pending`, `running`, `completed`, `failed`. When `completed`, `result` is the JSON-serialized skill output. When `failed`, `error` is a one-line `"{ExceptionClass}: {message}"` string.

### Task lifecycle

```
client                            agent
  |  POST /mesh/tasks/submit       |
  |------------------------------->|
  |                                | task_id, status=pending
  |  202 Accepted (task_id)        | asyncio.create_task(handler(payload))
  |<-------------------------------|
  |                                |  → status=running
  |                                |  → handler runs
  |  GET /mesh/tasks/{id}          |
  |------------------------------->|
  |  200 (status=running)          |
  |<-------------------------------|
  |   sleep(poll_interval)         |
  |  GET /mesh/tasks/{id}          |
  |------------------------------->|
  |                                |  → status=completed, result=...
  |  200 (status=completed)        |
  |<-------------------------------|
```

---

## Env vars

| Variable | Default | Purpose |
|---|---|---|
| `MESH_TASK_POLL_INTERVAL_SECONDS` | `0.5` | How often the orchestrator polls `/mesh/tasks/{id}` |
| `MESH_TASK_TIMEOUT_<SKILL_UPPERCASED>` | — | Per-skill deadline before `call_skill_blocking` raises `TaskTimeoutError` |
| `MESH_TASK_TIMEOUT_DEFAULT` | — | Fallback per-call timeout when no skill-specific override exists |
| `MESH_LLM_SKILL_TIMEOUT` | — | Legacy compat fallback (Phase 4); reused if no `MESH_TASK_TIMEOUT_*` is set |

Resolution order for the timeout: skill-specific → `MESH_TASK_TIMEOUT_DEFAULT` → `MESH_LLM_SKILL_TIMEOUT` → 120s.

LLM-bound skills (`extract_claims`, `challenge_belief`, `personalize_digest`) typically need much longer timeouts than the fast scouts (`scout_arxiv`, `scout_hn`). Set them explicitly in `.env` if you change the slow scouts:

```sh
MESH_TASK_TIMEOUT_EXTRACT_CLAIMS=120
MESH_TASK_TIMEOUT_CHALLENGE_BELIEF=180
MESH_TASK_TIMEOUT_SCOUT_ARXIV=60
```

---

## Skill reference

| Agent | Skill ID | Input fields | Output fields |
|---|---|---|---|
| ArXiv Scout | `scout_arxiv` | `categories`, `max_results`, `since` | `papers[]` |
| HN Scout | `scout_hn` | `keywords`, `max_results`, `min_points` | `papers[]` |
| Claim Extractor | `extract_claims` | `paper` (ScoutedPaper dict) | `claims[]`, `entities_referenced[]`, `latency_ms` |
| Entity Tracker | `resolve_entities` | `candidate_names[]`, `existing_entities[]`, `type_hints` | `resolved[]` (name, entity_id, is_new, …) |
| SOTA Tracker | `update_sota` | `claims[]`, `existing_sota_beliefs[]` | `belief_updates[]` |
| Curator | `select_beliefs_to_challenge` | `beliefs[]`, `pick_count`, `now`, `cooldown_days` | `picks[]` |
| Skeptic | `challenge_belief` | `belief`, `supporting_claims[]`, `contradicting_claims[]`, `in_scope_entities[]` | `verdict`, `confidence`, `rationale`, `suggested_confidence_delta`, `counter_claims[]` |

---

## Adding a new agent

1. Create `packages/mesh-agents/src/mesh_agents/my_agent.py`:
   - Implement the pure skill logic.
   - Add an `async def _handle_<skill>(payload: dict[str, Any]) -> dict[str, Any]` function that validates the payload, runs the skill, and returns a JSON-serializable dict.
   - Add a `to_a2a_server(url: str) -> Starlette` factory on the agent class that calls `build_task_app(agent_card=..., skill_handlers={"<skill_id>": _handle_<skill>}, agent_name="...")`.

2. Create `apps/agents/src/mesh_agent_servers/my_agent.py` (uvicorn entry point).

3. Add the service to `docker-compose.yml` and the URL to `MESH_AGENT_URLS` (or `MESH_SKEPTIC_AGENT_URLS` for falsification agents).

4. If there's a new orchestration step, wire it into `coordinator.py` or `skeptic_sweep.py` via `client.call_skill_blocking("<skill_id>", payload, traceparent=traceparent)`.

5. Add integration tests under `tests/integration/a2a/`.

---

## Package layout

```
packages/mesh-a2a/
  card_builder.py    — pure AgentCard construction helper
  client.py          — MeshA2AClient: discovery + submit_task + get_task + call_skill_blocking
  task_registry.py   — TaskRegistry (asyncio-safe in-memory task table)
  task_server.py     — build_task_app(agent_card, skill_handlers): Starlette factory
  tracing.py         — W3C traceparent encode/decode helpers

packages/mesh-agents/
  base.py            — BaseAgent (run() + to_a2a_server())
  arxiv_scout.py     — ArxivScoutAgent + _handle_scout_arxiv
  hn_scout.py        — HNScoutAgent + _handle_scout_hn
  claim_extractor.py — ClaimExtractorAgent + LLM-bound handler factory
  entity_tracker.py  — EntityTrackerAgent + _handle_resolve_entities + pure resolve_entities_pure
  sota_tracker.py    — SotaTrackerAgent + _handle_update_sota + pure update_sota_pure
  curator.py         — CuratorAgent + _handle_select_beliefs + pure score_belief
  skeptic.py         — SkepticAgent + LLM-bound handler factory

apps/agents/src/mesh_agent_servers/
  arxiv_scout.py     — uvicorn entry point, port 8001
  claim_extractor.py — uvicorn entry point, port 8002
  entity_tracker.py  — uvicorn entry point, port 8003
  sota_tracker.py    — uvicorn entry point, port 8004
  hn_scout.py        — uvicorn entry point, port 8005
  skeptic.py         — uvicorn entry point, port 8006
  curator.py         — uvicorn entry point, port 8007

apps/pipeline/src/mesh_pipeline/
  orchestrator.py    — Phase 1 in-process orchestrator (kept for tests / Phase 1 path)
  coordinator.py     — A2A coordinator; submits + polls via call_skill_blocking
  skeptic_sweep.py   — falsification orchestrator; same pattern
```
