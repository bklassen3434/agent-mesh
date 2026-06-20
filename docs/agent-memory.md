# Agent memory: episodic → outcome → procedural (Phase 16)

Phase 15 made agent **episodic memory** readable (what an agent did, and how it
fared). Phase 16 makes memory **load-bearing**: agents read their own history
and learned heuristics into their prompts, a procedural store captures those
heuristics, and a controller rule distills raw episodes into them offline.

```
 episodic (15)            procedural (16b)            consumption (16a/d)
 ───────────             ─────────────────           ────────────────────
 recall_history   ──▶    consolidation graph   ──▶   agent_heuristics  ──┐
 (claims +               (16c, batch LLM,            (agents schema)      │
  belief_revisions,       coordinator-written,                            │
  outcome tags)           low conf + TTL)                                 ▼
       │                                              build_memory_block prepends
       └───────────────────────────────────────────▶ heuristics + recent history
                                                      to the USER prompt (after the
                                                      cached system prefix)
```

## The pipeline

### 1. Episodic recall (consumed — 16a)

`recall_history(conn, agent, …)` (`packages/mesh-db/src/mesh_db/episodic.py`,
Phase 15) returns an agent's time-ordered actions, each tagged with a derived
**outcome** (`survived` / `contradicted` / `superseded` / `applied` / `held` /
…). Phase 16a folds a bounded, outcome-labelled block of this into the prompt of
the two **LLM-driven** skills that have a prompt to fold it into:

- `extract_claims` (claim_extractor)
- `challenge_belief` (skeptic)

The curator's `select_beliefs_to_challenge` and the `investigate_*` scout skills
are deterministic (rule-based scoring / keyword search) with **no LLM prompt**,
so there is nothing to fold a block into; they are intentionally left unchanged.

Agents read this from a **read-only `mesh_reader` connection**
(`get_connection(read_only=True)`); the read is best-effort and degrades to an
empty block when no reader DSN is configured (unit tests, minimal setups).
Coordinator-owned **writes** are untouched — recall is read-only.

### 2. Procedural store (16b)

`agents.agent_heuristics` + `agent_heuristic_revisions` (migration 008) model a
learned, revisable how-to, mirroring `belief` / `belief_revision`:

- **Coordinator-owned writes.** `mesh_writer` gets `SELECT/INSERT/UPDATE`; no
  `DELETE` (so the append-only / no-silent-overwrite invariant holds at the DB
  level, like claims). `mesh_reader` gets `SELECT` only. No agent role writes.
- **Revised append-only.** Every change writes a revision row
  (`mesh_pipeline._heuristics.persist_heuristic` writes a genesis revision on
  create; `revise_heuristic` appends and unions provenance).
- **Provenance mandatory.** Each heuristic links to the runs + claims that
  justify it (`provenance_run_ids` / `provenance_claim_ids`); a provenance-less
  proposal is refused (`MissingProvenanceError`).
- **TTL + low start.** New heuristics start at `confidence = 0.3` and carry an
  `expires_at`; consumption excludes expired and inactive rows.

Agents *propose* heuristics via the `propose_heuristic` skill contract
(`mesh_agents.consolidator`); only the controller persists, through the write
gateway via the `WriteHeuristicEffect`.

### 3. Consolidation (16c)

Memory consolidation is the controller's **`consolidate-memory`** skill (the
cooldown-gated `consolidatable_memory` tension). For each target `(agent, skill)`
it:

1. reads recent episodic + outcome history (`recall_history`),
2. distills candidate heuristics via an LLM call (`CONSOLIDATION_SYSTEM`; model
   env-routed for the `consolidator` role — `MESH_LLM_MODEL_CONSOLIDATOR` →
   default), run **synchronously** (the former Batch-API path is gone), and
3. emits a `WriteHeuristicEffect` per candidate with provenance (the runs +
   claims the history was drawn from), the low starting confidence, and a TTL;
   the gateway persists it via `persist_heuristic`.

It is **offline** — no LLM is added to the hot path. Identical active heuristics
are de-duplicated across runs.

### 4. Procedural consumption (16d)

`mesh_agents.memory.build_memory_block(agent, skill, …)` returns the combined
block: **scope-matched, unexpired, active heuristics first, then recent
history**. Heuristic scope is `(agent, skill)` plus optional finer `source` /
`entity_id` (`extract_claims` scopes to the paper's source type so
source-specific how-to applies; `challenge_belief` scopes recall to the belief
topic). `list_applicable_heuristics` excludes anything past `expires_at` or
inactive.

## Prompt-cache placement rule (do not break)

The Anthropic client marks **only the system prompt** with
`cache_control={"type": "ephemeral"}` — that is the cached prefix
(`mesh_llm.anthropic_client`). All per-call memory (heuristics + history) is
therefore added to the **user** message, *after* the cached system prefix. The
block is prepended to the task content within the user message (giving the
plan's order: heuristics → recent history → task), but it never touches the
system prompt, so the Phase-11 prompt-cache prefix is unchanged.

## Cadence

Consolidation runs as the controller's `consolidate-memory` skill, fired by the
cooldown-gated `consolidatable_memory` tension (one per field, gated by
`MESH_CONTROLLER_MAINTAIN_COOLDOWN_SEC`, 24h default) — the same
"temporal-condition = state-condition" pattern as `scout-when-idle`. **No
separate service, container, or scheduled job.**

## Inspection

```bash
uv run mesh.cli heuristics list                      # all unexpired heuristics
uv run mesh.cli heuristics list --agent skeptic      # by agent
uv run mesh.cli heuristics list --skill extract_claims --include-expired
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `MESH_CONSOLIDATION_HISTORY_LIMIT` | `50` | Episodic entries per target fed to distillation |
| `MESH_CONSOLIDATION_TTL_DAYS` | `30` | TTL stamped on distilled heuristics |
| `MESH_CONSOLIDATION_TARGETS` | `claim_extractor:extract_claims,skeptic:challenge_belief` | `(agent:skill)` pairs to consolidate |
| `MESH_LLM_MODEL_CONSOLIDATOR` | (falls back to default) | Model for the consolidator role |
| `MESH_PG_READER_URL` | (falls back to base DSN) | Read-only DSN agents use to recall memory |

## Scope notes (honest negatives)

- Only the two LLM skills consume memory; the rule-based curator and the
  search-only `investigate_*` skills do not (no prompt to fold into).
- Consolidation distills only agent-attributed history (`claim_extractor`,
  `skeptic`). Scouts produce no agent-attributed artifacts, so they have no
  episodic history to learn from (see `docs/episodic-memory.md`).
- Heuristic de-dup is exact-text within scope; semantic similarity / pgvector
  retrieval is explicitly out of scope this phase.
