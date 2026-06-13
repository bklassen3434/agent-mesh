# Phase 20 — Model Routing: Tiered Models with Difficulty-Based Escalation

## Context

Agent Mesh runs every LLM call through one of two interchangeable clients —
`AnthropicClient` (default, Haiku 4.5) and `OllamaClient` (local) — selected by
`make_llm_client()` in `packages/mesh-llm/src/mesh_llm/factory.py`. The factory
already supports **static per-agent model selection**: `resolve_model(agent_name,
default)` consults, in precedence order, `MESH_LLM_MODEL_<AGENT>` →
`MESH_LLM_MODEL_DEFAULT` → `MESH_LLM_MODEL` → the provider's hard default. So an
operator can already pin, say, the skeptic to a bigger model via
`MESH_LLM_MODEL_SKEPTIC`.

What does **not** exist is *dynamic, per-request* routing: today every call from
a given agent uses one fixed model regardless of how hard the specific input is.
That leaves money on the table (a trivial abstract and a dense
twelve-benchmark-table paper both go to the same model) and quality on the table
(a genuinely hard extraction silently gets the cheap model and produces a thin
result). The goal of this phase is a **router** that sends the bulk of traffic to
a cheap tier and *escalates* the hard or failed cases to a strong tier — without
changing any agent's logic and without disturbing the existing static-override
path.

This is **infrastructure, not a feature**: it lives almost entirely in
`mesh-llm`, is **purely additive** (existing `make_llm_client()` and every
current call site keep working unchanged), and is **opt-in per agent**. Because
it touches the layer every LLM-using feature depends on, it is built so that no
other in-flight phase has to wait for it or rebase on it.

Read before writing any code — do not guess signatures or call sites:

- `mesh_llm.protocol.LLMClient` — the Protocol every client implements
  (`model: str`, `health_check`, `complete_with_latency`, `complete_with_usage`);
  a router must implement this same Protocol so it is a drop-in.
- `mesh_llm.factory` — `make_llm_client(provider, agent_name)` and
  `resolve_model(agent_name, default)`; the env precedence chain.
- `mesh_llm.anthropic_client.AnthropicClient` / `mesh_llm.client.OllamaClient` —
  constructors (both take `model`, `agent_name`), `LLMResponseError`,
  `AnthropicNotReadyError` / `OllamaNotReadyError` /
  `LLMProviderNotReadyError`, the `cache_control` system-prefix handling, and the
  batch surface (`submit_batch`/`batch_status`/`collect_batch`).
- `mesh_llm.usage.LLMUsage` and `mesh_llm.pricing` (`estimate_cost`, `is_priced`,
  `CostBreakdown`) — so routing decisions are costed and the savings are
  measurable.
- The eight LLM call sites (all `make_llm_client(agent_name=...)`):
  `apps/agents/.../{claim_extractor,skeptic,personalizer}.py`,
  `apps/pipeline/.../orchestrator.py`,
  `apps/pipeline/.../coordinator.py` (entity-resolution adjudication),
  `apps/pipeline/.../skeptic_sweep.py`,
  `apps/pipeline/.../consolidation.py`, and the `apps/cli` backfill command.
- `mesh_tracing.trace_generation` and the `llm_usage` ledger
  (`mesh_db.llm_usage`, written from coordinator/sweep finalize) — where the
  chosen `model` is already recorded per skill, so escalations are observable
  with no schema change.

---

## Goal

A `RoutedLLMClient` that implements `LLMClient` and, per request, picks a **cheap
tier** by default and **escalates to a strong tier** when a configurable
difficulty signal fires or the cheap attempt fails to parse. A new additive
factory `make_routed_llm_client(agent_name=...)` returns it; agents opt in by
switching their one construction line. Routing is env-configured (tier model ids,
per-agent enable, escalation triggers), every decision is traced + costed, and
with routing disabled the system behaves exactly as today.

---

## Principles (do not violate)

- **Additive, never a breaking change.** Do **not** change the signature or
  behavior of `make_llm_client` or `resolve_model`. The router is a new
  Protocol-conforming class plus a new factory. Every existing call site compiles
  and runs unchanged; a phase mid-flight that constructs an LLM client is
  unaffected.
- **The router IS an `LLMClient`.** It satisfies the same Protocol
  (`complete_with_latency` / `complete_with_usage` / `health_check` / `model`),
  so agents, the batch path, and tracing treat it as any other client. No agent
  learns it is being routed.
- **Cheap-first, escalate on signal — never the reverse.** The default path is
  the cheap tier. Escalation is the exception and must be explainable (a recorded
  reason). Never silently downgrade a request an operator pinned to a strong
  model via the existing static override.
- **Fail safe, not silent.** A cheap-tier `LLMResponseError` (parse failure)
  triggers one retry on the strong tier; a strong-tier failure surfaces normally.
  Provider-not-ready (`AnthropicNotReadyError` / `OllamaNotReadyError`)
  propagates — routing never swallows an unconfigured-provider error.
- **Every routing decision is observable.** The chosen model is already recorded
  in `llm_usage.model` per call; additionally attach the tier + escalation reason
  to the Langfuse generation metadata via `trace_generation`. Escalations must be
  countable after the fact.
- **Off by default; deterministic when off.** With routing disabled (no
  `MESH_ROUTE_*` config, or `MESH_ROUTE_ENABLED=false`), `make_routed_llm_client`
  returns a plain client identical to today's. No nondeterministic model choice
  ever reaches a test without explicit config.
- **Difficulty heuristics are cheap and local.** Request-classification must not
  itself call an LLM on the hot path (that would defeat the savings). Use
  input-size / structure features and (optionally) a tiny local signal, not a
  model call, to decide the tier.

---

## Scope

### 1. Tier config + routing policy — block 20a

The model of "which model for which request", with no client wiring yet.

- `packages/mesh-llm/src/mesh_llm/routing.py`:
  - `Tier` (cheap | strong) and a `RoutingConfig` read from env:
    - `MESH_ROUTE_ENABLED` (default `false`).
    - `MESH_ROUTE_CHEAP_MODEL` (default: the provider's current default —
      `claude-haiku-4-5` for Anthropic, `qwen3:8b`/local for Ollama).
    - `MESH_ROUTE_STRONG_MODEL` (default `claude-sonnet-4-6`; an operator may set
      `claude-opus-4-8` for the hardest fields).
    - `MESH_ROUTE_CHEAP_PROVIDER` / `MESH_ROUTE_STRONG_PROVIDER` (optional —
      lets the cheap tier be local Ollama while the strong tier is the Anthropic
      API; default: both = `MESH_LLM_PROVIDER`).
    - Per-agent enable + per-agent strong-model override
      (`MESH_ROUTE_<AGENT>_ENABLED`, `MESH_LLM_MODEL_<AGENT>_STRONG`) so the
      skeptic can escalate to Opus while extraction escalates only to Sonnet.
  - `classify_difficulty(name, system, user, options) -> Tier` — a pure,
    LLM-free heuristic. Start simple and documented: escalate to `strong` when
    the user content exceeds `MESH_ROUTE_ESCALATE_CHARS` (default tuned to a long
    paper / dense table), when `options` carries an explicit
    `route_hint="strong"`, or when the calling agent's policy marks the skill as
    hard. Otherwise `cheap`. Keep the rule set small and explainable.
  - `RoutingDecision` (chosen `Tier`, model id, provider, reason string) — the
    object traced + (optionally) logged.
- Respect the existing static override: if `MESH_LLM_MODEL_<AGENT>` /
  `MESH_LLM_MODEL` pins a model, routing is bypassed for that agent (an operator
  pin wins). Document this precedence explicitly.

**Exit:** `RoutingConfig.from_env()` round-trips all knobs with the documented
defaults; `classify_difficulty` returns `strong` for over-threshold / hinted
inputs and `cheap` otherwise; the static-override bypass is unit-tested; `ruff` +
`mypy --strict` clean. Tag `v0.20.0-phase-20a`.

### 2. RoutedLLMClient — block 20b

The Protocol-conforming wrapper that actually routes.

- `RoutedLLMClient` in `mesh_llm.routing` implementing `LLMClient`:
  - Constructs (lazily) a cheap client and a strong client via the existing
    `AnthropicClient` / `OllamaClient` constructors (passing the tier's model +
    provider). `model` (the Protocol attribute) reports the cheap-tier model.
  - `complete_with_latency` / `complete_with_usage`: call
    `classify_difficulty`; run the chosen tier; on a cheap-tier
    `LLMResponseError`, retry once on the strong tier (configurable via
    `MESH_ROUTE_ESCALATE_ON_PARSE_FAIL`, default `true`); record the
    `RoutingDecision`. Latency/usage returned are the tier that actually
    answered.
  - `health_check`: checks the tier(s) that will be used; a strong-tier-only
    misconfiguration that the run may never hit should not hard-fail startup
    (document the chosen posture).
- Tracing: pass the chosen model + tier + reason to `trace_generation`
  (metadata), so per-tier volume and escalation rate are queryable in Langfuse;
  the `llm_usage.model` column already captures the realized model per skill —
  **no schema change, no migration.**
- Batch path: routing is a *hot-path / sync* concern. The Anthropic Batch API
  (`submit_batch`) is used by `skeptic_sweep` / `consolidation` /
  `belief_consolidation` and submits a homogeneous model. For this phase, batch
  jobs choose their tier **once per batch** (cheap by default, an env to force
  strong) rather than per-item; document that per-item batch routing is out of
  scope. The router exposes the strong client so a batch caller can opt in.

**Exit:** a routed client sends an under-threshold request to the cheap tier and
an over-threshold (or parse-failed) request to the strong tier, returns correct
latency/usage, and records the decision; with routing disabled it is
behaviorally identical to a plain client; unit-tested with mock cheap/strong
clients (including the parse-fail → escalate path); `ruff` + `mypy --strict`
clean. Tag `v0.20.0-phase-20b`.

### 3. Factory + opt-in wiring — block 20c

Expose the router and switch the agents that benefit, leaving everything else
untouched.

- `make_routed_llm_client(provider=None, agent_name=None) -> LLMClient` in
  `mesh_llm.factory`: when routing is enabled for `agent_name`, return a
  `RoutedLLMClient`; otherwise return `make_llm_client(provider, agent_name)`
  unchanged. Export from `mesh_llm.__init__`.
- Opt in the high-volume, variance-heavy agents by switching their single
  construction line from `make_llm_client(agent_name=...)` to
  `make_routed_llm_client(agent_name=...)`:
  - `claim_extractor` (highest volume; most input-size variance) — the primary
    win.
  - the coordinator's entity-resolution adjudication (already best-effort;
    cheap-first with escalate-on-ambiguity fits its existing posture).
  - `skeptic` sync path.
  Leave `personalizer`, the Phase-1 `orchestrator`, and the CLI backfill on the
  plain factory unless a clear win is shown. **Do not** touch any phase still in
  flight; switching is a one-line, reversible change per agent.
- Document the default deployment recipe in `.env.example`: routing **off** by
  default; a commented block showing cheap=Haiku 4.5 / strong=Sonnet 4.6 with
  extraction + skeptic enabled.

**Exit:** `make_routed_llm_client` returns a router when enabled and a plain
client otherwise; the three opted-in agents construct via it; an end-to-end
pipeline run with routing **on** produces correct output with a measurable cheap-
vs-strong split in `llm_usage.model`; with routing **off** the run is byte-for-
byte the prior behavior; `ruff` + `mypy --strict` clean; existing pytest
unaffected. Tag `v0.20.0-phase-20c`.

### 4. Cost visibility + docs — block 20d

Make the savings legible and write it down.

- A read-only CLI summary `mesh.cli routing-stats [--field <slug>] [--since …]`
  mirroring `pipeline-stats`: aggregate `llm_usage` by `model` (and, where
  recorded, tier) to show request counts, token totals, and `estimate_cost`
  dollars per tier — the before/after evidence that routing is paying off. (No
  new table; reads the existing ledger.)
- `docs/model-routing.md`: the tier model, the difficulty heuristic and how to
  tune it, the static-override precedence, the escalate-on-parse-fail behavior,
  the batch caveat, and the trust/cost trade-offs. Match `docs/llm-setup.md`
  style. Update `CLAUDE.md`'s phase-status paragraph + env-var table with the
  `MESH_ROUTE_*` knobs.

**Exit:** `mesh.cli routing-stats` reports per-tier request/token/cost splits
from the existing ledger; `docs/model-routing.md` added; `CLAUDE.md` updated;
`ruff` + `mypy --strict` clean. Tag `v0.20.0-phase-20d`.

---

## Out of Scope (do not build)

- **LLM-based difficulty classification on the hot path.** A model call to decide
  which model to call defeats the purpose; difficulty is heuristic + local only.
- **Per-item routing inside an Anthropic Batch.** Batch jobs pick a tier per
  batch this phase.
- **A new provider, a new client class beyond the router, or any change to the
  `LLMClient` Protocol.**
- **A routing-decisions table / new migration.** Observability rides the existing
  `llm_usage.model` column + Langfuse metadata.
- **Auto-tuning / learned routing (bandits, RL, feedback loops from belief
  outcomes).** Static, explainable heuristics only; learned routing is a later
  phase.
- **Changing the default — routing ships OFF.** No behavior change for anyone who
  doesn't opt in.

---

## Exit Criteria

- [ ] `routing.py` adds `RoutingConfig` (env), `Tier`, `classify_difficulty`
      (pure, LLM-free), and `RoutingDecision`; static `MESH_LLM_MODEL_<AGENT>`
      override bypasses routing
- [ ] `RoutedLLMClient` implements the `LLMClient` Protocol, routes cheap-first,
      escalates on threshold/hint and on cheap-tier parse failure, and propagates
      provider-not-ready errors
- [ ] `make_routed_llm_client` is additive; `make_llm_client` / `resolve_model`
      are unchanged; with routing off, behavior is identical to today
- [ ] `claim_extractor`, entity-resolution adjudication, and the skeptic sync
      path opt in via the new factory; no other call site changed
- [ ] Each routing decision is traced (tier + reason) and the realized model is
      visible in `llm_usage.model`; **no new table / migration**
- [ ] `mesh.cli routing-stats` shows per-tier request/token/cost splits
- [ ] `docs/model-routing.md` added; `CLAUDE.md` phase status + env table updated
- [ ] `ruff` + `mypy --strict` clean across touched packages; existing pytest +
      Playwright unaffected
- [ ] No `LLMClient` Protocol change; no provider added; coordinator-owned writes
      and all role grants unchanged

---

## Commit Convention

One logical commit per unit; conventional messages:

```
feat(llm): add tiered RoutingConfig + difficulty classifier
feat(llm): add RoutedLLMClient (cheap-first, escalate-on-signal/parse-fail)
feat(llm): add make_routed_llm_client; opt in extraction/skeptic/resolution
feat(cli): add routing-stats per-tier cost summary
docs: add model-routing.md; update CLAUDE.md
```

Tags map to blocks: `v0.20.0-phase-20a` (config + policy), `…-20b` (router),
`…-20c` (factory + opt-in wiring), `…-20d` (cost visibility + docs). Execute
20a → 20d in order. Lint, types, and a clean routing-off run are the bar before
each tag. Report any principle conflict (e.g. a call site that can't adopt the
router without a signature change) before working around it.
