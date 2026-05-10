# A2A Protocol Layer

This document explains how Agent Mesh uses the [Agent2Agent (A2A) protocol](https://a2aproject.github.io/A2A/) to turn local Python classes into distributed HTTP agents.

---

## Why A2A?

Phase 1 ran all four agents in the same process. That works but limits horizontal scaling and prevents independent deployment. A2A gives each agent a standardized HTTP interface so any compatible client can discover and call it without knowing its implementation language or framework.

---

## Key architectural decisions

### Coordinator owns all DB writes

In Phase 2 the coordinator is the only process that touches DuckDB. Agents are pure functions: they receive typed JSON input and return typed JSON output with no side effects.

**Why:** DuckDB supports only one writer at a time. Routing all writes through the coordinator keeps the constraint trivially satisfied and makes the coordinator the single source of truth.

**How it works in practice:**
- Before calling `resolve_entities`, the coordinator fetches all existing entities from DB and passes them in the request.
- Before calling `update_sota`, the coordinator fetches existing SOTA beliefs and passes them.
- After each skill call, the coordinator persists the results (new entities, claims, belief revisions).

### Real capability discovery (no hardcoded routing)

The coordinator never imports agent classes. On startup it fetches `/.well-known/agent-card.json` from each configured base URL and builds a `skill_id → agent_url` map. Dispatch logic only ever calls `call_skill(skill_id, payload)`.

**Why:** This is the point of A2A. If you hardcode URLs in dispatch logic you've just done HTTP-based in-process calls with extra steps. Real discovery lets you swap agents, add new implementations, or route to remote agents without touching the coordinator.

### Sync request/response only

All agents declare `capabilities.streaming = False`. The coordinator uses the non-streaming `send_message` path (single request → single response with artifacts).

**Why:** The pipeline tasks are short-lived (seconds to tens of seconds). SSE streaming adds protocol complexity for no benefit here. Phase 3+ can revisit this if agents need to stream incremental results.

### No auth (Phase 2)

Agent cards declare no security schemes. The coordinator does not send credentials.

**Why:** The network is trusted (docker-compose internal network). Phase 6 will add Bearer token or mTLS.

```python
# TODO(phase-6): auth — add securitySchemes to card_builder.build_agent_card() and
# a matching AuthInterceptor on the coordinator client once we need real auth.
```

### W3C traceparent for distributed tracing

The coordinator generates a `traceparent` header for each pipeline run (format: `00-{trace_id}-{parent_id}-01`). Every outbound `SendMessageRequest` carries this in `metadata['traceparent']`. Agent executors extract it from `context.metadata` and attach their Langfuse generations to the same trace ID, producing a single trace tree in Langfuse per pipeline run.

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

### Skill call (JSON-RPC 2.0)

The coordinator sends a `message/send` JSON-RPC call with a data Part containing the skill input:

```json
{
  "jsonrpc": "2.0",
  "id": "...",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [{ "data": { "candidate_names": ["GPT-4"], "existing_entities": [] } }],
      "messageId": "...",
      "taskId": "...",
      "contextId": "..."
    },
    "metadata": { "traceparent": "00-abc123...-def456...-01" }
  }
}
```

The agent returns a task with a data artifact:

```json
{
  "result": {
    "kind": "task",
    "id": "...",
    "status": { "state": "completed" },
    "artifacts": [
      {
        "name": "result",
        "parts": [{ "data": { "resolved": [{ "name": "GPT-4", "entity_id": "...", "is_new": true }] } }]
      }
    ]
  }
}
```

---

## Skill reference

| Agent | Skill ID | Input fields | Output fields |
|---|---|---|---|
| ArXiv Scout | `scout_arxiv` | `categories`, `max_results`, `since` | `papers[]` |
| Claim Extractor | `extract_claims` | `paper` (ScoutedPaper dict) | `claims[]`, `entities_referenced[]`, `latency_ms` |
| Entity Tracker | `resolve_entities` | `candidate_names[]`, `existing_entities[]`, `type_hints` | `resolved[]` (name, entity_id, is_new, …) |
| SOTA Tracker | `update_sota` | `claims[]`, `existing_sota_beliefs[]` | `belief_updates[]` |

---

## Adding a new agent

1. Create `packages/mesh-agents/src/mesh_agents/my_agent.py`:
   - Implement the pure skill function (no DB access)
   - Add an `AgentExecutor` subclass that reads from `context.message.parts[0].data` and writes to artifacts
   - Add a `to_a2a_server(url: str) -> Starlette` factory on the agent class

2. Create `apps/agents/src/mesh_agent_servers/my_agent.py` (entry point with uvicorn).

3. Add the service to `docker-compose.yml` and the URL to `MESH_AGENT_URLS`.

4. Add a skill handler call in `apps/pipeline/src/mesh_pipeline/coordinator.py`.

5. Add integration tests under `tests/integration/a2a/`.

---

## Package layout

```
packages/mesh-a2a/
  card_builder.py   — pure AgentCard construction helper
  client.py         — MeshA2AClient: discovery + skill dispatch + traceparent injection
  tracing.py        — W3C traceparent encode/decode helpers

packages/mesh-agents/
  base.py           — BaseAgent (run() + to_a2a_server())
  arxiv_scout.py    — ArxivScoutAgent + _ArxivScoutExecutor
  claim_extractor.py — ClaimExtractorAgent + _ClaimExtractorExecutor
  entity_tracker.py — EntityTrackerAgent + _EntityTrackerExecutor + pure resolve_entities_pure()
  sota_tracker.py   — SotaTrackerAgent + _SotaTrackerExecutor + pure update_sota_pure()

apps/agents/src/mesh_agent_servers/
  arxiv_scout.py    — uvicorn entry point, port 8001
  claim_extractor.py — uvicorn entry point, port 8002
  entity_tracker.py — uvicorn entry point, port 8003
  sota_tracker.py   — uvicorn entry point, port 8004

apps/pipeline/src/mesh_pipeline/
  orchestrator.py   — Phase 1 in-process orchestrator (unchanged)
  coordinator.py    — Phase 2 A2A coordinator
```
