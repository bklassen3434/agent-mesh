# Model Routing (Phase 20)

Tiered models with difficulty-based escalation. A **router** sends the bulk of
LLM traffic to a **cheap tier** and **escalates** the hard or failed cases to a
**strong tier** — without changing any agent's logic.

Routing is **infrastructure, not a feature**: it lives almost entirely in
`mesh-llm`, is **purely additive** (every existing call site keeps working
unchanged), is **opt-in per agent**, and **ships off by default**. With routing
disabled the system behaves exactly as before.

| | Default | Typical use |
|---|---|---|
| Cheap tier | the provider default (`claude-haiku-4-5` / `qwen3:8b`) | the common path — most requests |
| Strong tier | `claude-sonnet-4-6` (operator may set `claude-opus-4-8`) | long/dense inputs, hinted-hard requests, cheap-tier parse failures |

The router is itself an `LLMClient` (same Protocol as `AnthropicClient` /
`OllamaClient`), so agents, the batch path, and tracing treat it like any other
client. No agent learns it is being routed.

---

## Turning it on

Routing is returned by the additive factory `make_routed_llm_client(agent_name=…)`
only when **both** hold:

1. **No static model pin exists for the agent** (see precedence below).
2. **Routing is enabled** for the agent.

Recommended recipe in `.env` — cheap=Haiku 4.5, strong=Sonnet 4.6, routing the
high-volume, input-size-variance agents (extraction + skeptic + entity
resolution):

```
MESH_ROUTE_ENABLED=true
MESH_ROUTE_CHEAP_MODEL=claude-haiku-4-5
MESH_ROUTE_STRONG_MODEL=claude-sonnet-4-6
MESH_ROUTE_EXTRACTION_ENABLED=true
MESH_ROUTE_SKEPTIC_ENABLED=true
MESH_ROUTE_ENTITY_RESOLUTION_ENABLED=true
```

> ⚠️ **A static pin bypasses routing.** `MESH_LLM_MODEL` in the default
> `.env.example` is an explicit operator pin and wins over routing. To let an
> agent route, leave its model unpinned (comment `MESH_LLM_MODEL` out).

The three opted-in call sites are `claim_extractor` (highest volume, most input
variance — the primary win), the controller's entity-resolution adjudication
(already best-effort; cheap-first with escalate-on-ambiguity fits its posture),
and the skeptic challenge path. The personalizer and the CLI backfill stay on the
plain factory.

---

## The difficulty heuristic

Request classification is **pure and LLM-free** — a model call to decide which
model to call would defeat the savings. The rule set is intentionally small and
explainable (`classify_difficulty` in `mesh_llm.routing`). A request escalates
to the strong tier when:

- `options["route_hint"] == "strong"` — a caller marks this specific request
  hard (an explicit `"cheap"` hint pins the cheap tier even for long inputs); or
- the **user content length** reaches `MESH_ROUTE_ESCALATE_CHARS`
  (default `12000` chars ≈ a long paper / dense multi-benchmark table).

Otherwise the request goes to the cheap tier. Every decision records a
human-readable `reason` ("user content 13402 chars ≥ 12000", "route_hint=strong",
"default cheap", "cheap parse failure → escalate").

**Tuning:** lower `MESH_ROUTE_ESCALATE_CHARS` to escalate more aggressively (more
quality, more cost); raise it to keep more traffic cheap. Watch the cheap-vs-
strong split with `mesh.cli routing-stats` (below) and adjust.

---

## Escalate on parse failure

A cheap-tier `LLMResponseError` (the model returned output that wouldn't parse
into the expected schema) triggers **one retry on the strong tier**
(`MESH_ROUTE_ESCALATE_ON_PARSE_FAIL`, default `true`). This is fail-safe, not
silent: the escalation is recorded with reason `"cheap parse failure →
escalate"`. A **strong-tier** failure surfaces normally — there is no retry loop.

Provider-not-ready errors (`AnthropicNotReadyError` / `OllamaNotReadyError`)
**always propagate** — routing never swallows an unconfigured-provider error.

`health_check()` checks only the **cheap tier** (the default path every enabled
run exercises). The strong tier is built lazily on first escalation, so a
strong-tier-only misconfiguration a run may never hit does not hard-fail startup.

---

## Static-override precedence

An explicit operator pin always wins and is **never** silently downgraded to the
cheap tier. Mirroring `resolve_model`, any of these counts as a pin and bypasses
routing for the agent:

1. `MESH_LLM_MODEL_<AGENT>` — per-agent pin
2. `MESH_LLM_MODEL_DEFAULT` — workspace-wide pin
3. `MESH_LLM_MODEL` — legacy single-model pin

Routing knobs:

| Variable | Default | Purpose |
|---|---|---|
| `MESH_ROUTE_ENABLED` | `false` | Global routing switch |
| `MESH_ROUTE_<AGENT>_ENABLED` | (inherits global) | Per-agent enable; overrides the global flag |
| `MESH_ROUTE_CHEAP_MODEL` | provider default | Cheap-tier model id |
| `MESH_ROUTE_STRONG_MODEL` | `claude-sonnet-4-6` | Strong-tier model id |
| `MESH_LLM_MODEL_<AGENT>_STRONG` | (falls back to `MESH_ROUTE_STRONG_MODEL`) | Per-agent strong-model override |
| `MESH_ROUTE_CHEAP_PROVIDER` / `MESH_ROUTE_STRONG_PROVIDER` | `MESH_LLM_PROVIDER` | Per-tier provider (e.g. cheap local Ollama, strong Anthropic API) |
| `MESH_ROUTE_ESCALATE_CHARS` | `12000` | User-content length threshold for escalation |
| `MESH_ROUTE_ESCALATE_ON_PARSE_FAIL` | `true` | Retry once on strong when the cheap tier fails to parse |

---

## The batch caveat

Routing is a **hot-path / sync** concern. Where the Anthropic Batch API
(`submit_batch`) is still used, a batch submits a homogeneous model, so it chooses
its tier **once per batch** (cheap by default), not per item. Per-item batch
routing is out of scope for this phase. The router exposes its `strong_client` so a
batch caller can opt a whole batch into the strong tier. (The controller's skills —
including the migrated skeptic, memory-consolidation, and belief-consolidation
paths — now run synchronously, so routing applies per call there.)

---

## Observability

Every routing decision is observable with **no schema change and no migration**:

- The **realized model** is already recorded per call in `llm_usage.model` (the
  ledger written from the controller's finalize). Per-tier volume is therefore
  queryable straight from the ledger.
- The **tier + escalation reason** are additionally attached to the Langfuse
  generation metadata (the router threads them through a reserved options key
  that both clients strip before the wire and forward to `trace_generation`), so
  per-tier volume and escalation rate are queryable in Langfuse.

### `mesh.cli routing-stats`

The before/after evidence that routing is paying off — per-tier request counts,
token totals, and estimated dollars, read from the existing ledger:

```bash
uv run mesh.cli routing-stats                 # all fields, all time
uv run mesh.cli routing-stats --field ai-robotics --since 7d
```

Tier isn't persisted (only the realized `model` is), so the command derives each
row's tier by matching the live `RoutingConfig`'s cheap/strong models first, then
a Claude-family fallback (haiku → cheap, sonnet/opus → strong). Run it before and
after enabling routing on the same workload to see the cheap-vs-strong split and
the cost delta.

---

## Trust / cost trade-offs

- **Cheap-first is the bet that most requests are easy.** It pays off when the
  bulk of traffic is short/simple and only a minority is genuinely hard. Validate
  with `routing-stats`: if nearly everything escalates, the threshold is too low
  or the workload doesn't fit the cheap tier — pin a single strong model instead.
- **Escalate-on-parse-fail trades a little latency + cost for resilience.** A
  cheap-tier schema miss gets a strong-tier second chance instead of being
  dropped. Disable it (`MESH_ROUTE_ESCALATE_ON_PARSE_FAIL=false`) if you'd rather
  surface the failure than pay for the retry.
- **The heuristic is static and explainable by design.** Learned / auto-tuned
  routing (bandits, feedback from belief outcomes) is deliberately out of scope —
  a later phase. Tune the one threshold and the per-agent enables by hand.
