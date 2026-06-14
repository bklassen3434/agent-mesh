# Roadmap — Phases 18–23: Parallelization & Launch Guide

> **Status (historical planning doc).** Phases 19–23 have shipped (belief
> consolidation, model routing, knowledge chatbot, autonomous discovery, agent
> observability — each has its own doc and is summarized in
> [`architecture.md`](architecture.md)). Phase 18's connector *catalog* shipped
> (it backs the field-agnostic core, see [`field-agnostic.md`](field-agnostic.md)),
> but the self-serve onboarding UX remains deferred. This file is kept as a record
> of how the work was parallelized.

This is the coordination doc for six features specced as Phases 18–23. It answers
one question: **can these run in parallel across separate Conductor workspaces,
and what must be coordinated?** Short answer: **yes — all six can run
concurrently**, with a handful of well-defined touchpoints below.

The six specs:

| Phase | Feature | Spec | User task |
|---|---|---|---|
| **18** | Self-Serve Connectors + Field Onboarding | `docs/phase-18-connectors-onboarding.md` *(already written)* | "Self-serve connectors" |
| **19** | Belief Consolidation (scheduled merge + decay) | `docs/phase-19-belief-consolidation.md` | "Belief consolidation system" |
| **20** | Model Routing (tiered, difficulty-based) | `docs/phase-20-model-routing.md` | "Model routing" |
| **21** | Knowledge Chatbot (grounded Q&A) | `docs/phase-21-knowledge-chatbot.md` | "Asking questions to chatbot" |
| **22** | Autonomous Discovery (self-directed investigation) | `docs/phase-22-autonomous-discovery.md` | "Make the system more agentic" |
| **23** | Agent Observability (inspect agent thinking/memory) | `docs/phase-23-agent-observability.md` | "Observability into what an agent is thinking" |

> **Numbering note.** The pasted spec titled "Phase 17 — Belief Consolidation" was
> an earlier draft; the *current* repo already shipped Phase 17 (field isolation)
> and Phase 18 (connectors). So belief consolidation is renumbered to **Phase 19**
> and its migration moves from `009` → **`011`** / tags to `v0.19.0-*`. The
> connectors spec keeps its number (18) — it already exists in `docs/`.

---

## Verdict: parallelizable

All six are **independently buildable today**. None has a *hard* blocking
dependency on another. Two have *soft* dependencies (they get better, not
unblocked, when a sibling lands) and are designed in their specs to start against
what exists and adopt the sibling later.

```
Hard dependencies:   none
Soft dependencies:   22 ──benefits──▶ 18 (web_search connector as a search tool)
                     22 ──benefits──▶ 20 (cheap routing for frequent gap analysis)
                     21 ──optional──▶ 19 (reuse belief embeddings; FTS path avoids it)
                     19/22/23 surface in ─▶ 23 (new agents auto-appear; no dep)
```

Every "benefits/optional" arrow is handled in the dependent spec with a
graceful fallback, so **you can launch all six at once.**

### Recommended launch waves (optional, not required)

If you'd rather stagger slightly to minimize merge friction, the cleanest split:

- **Wave 1 — start immediately, zero coupling:** **18, 19, 20, 23.**
  - 18 & 19 are already-shaped specs with isolated surfaces.
  - 20 is additive infra in `mesh-llm` (ships OFF; touches nobody else).
  - 23 records/reads through the existing dispatch path; depends on nothing new.
- **Wave 2 — start now too, but rebase on Wave 1 as it merges:** **21, 22.**
  - 21 (chatbot) is fully independent via its FTS path; only *optionally* reuses
    19's belief embeddings. Safe to start in parallel; rebase if you want vectors.
  - 22 (discovery) works against existing connectors; adopt 18's `web_search` and
    20's routing when they land.

Launching all six simultaneously is fine — Wave 2 just carries a slightly higher
rebase cost on the shared files below.

---

## The one thing to coordinate up front: migration numbers

Numbered SQL migrations (`packages/mesh-db/migrations_pg/NNN_*.sql`) are the only
resource where parallel workspaces *will* collide if uncoordinated. `010` is the
current highest. **Pre-allocated, non-overlapping numbers** (already written into
each spec):

| Phase | Migration | Adds | Has DELETE grant? |
|---|---|---|---|
| 18 | **none** | new connectors are seeded rows via `seed_connectors` (code, idempotent); new `SourceType`s are Python enums | — |
| 20 | **none** | routing rides the existing `llm_usage.model` column + Langfuse metadata | — |
| 19 | **`011`** | `beliefs.statement_embedding vector(384)` + HNSW index | **no** (deliberate) |
| 21 | **`012`** | GIN `tsvector` indexes on beliefs/claims/entities (read-only FTS) | — |
| 22 | **`013`** | `investigations.origin` + `trigger_rationale` (+ index), backfill | — |
| 23 | **`014`** | `agent_invocations` table (append-only, field-scoped) | **no** |

Rules:
- Migrations apply in numeric order regardless of merge order; these four
  (`011`–`014`) touch disjoint tables, so order doesn't matter and gaps (if a
  phase slips) are harmless.
- **If a workspace needs an unplanned migration, claim the next free number and
  announce it** (bump this table) before writing the file. Two `011`s is the one
  failure mode to avoid.

---

## Shared-file touchpoints (expect trivial merge conflicts)

These are *edits to the same file* by multiple phases — small, mechanical to
resolve, not architectural. Listed so each workspace expects them.

| File | Edited by | Nature | Severity |
|---|---|---|---|
| `CLAUDE.md` (phase-status ¶ + env table) | **all** | append a sentence + a few env rows | trivial |
| `apps/api/.../main.py` (`include_router`) | 18, 21, 23 | add one router line each | trivial |
| `apps/wiki/src/components/nav*.tsx` | 18, 21, 23 | add a nav entry each | low |
| `apps/wiki/src/lib/api-types.ts` (generated) | 18, 21, 23 | regenerate via `make types` on merge | trivial (don't hand-edit) |
| `apps/scheduler/.../scheduler.py` `JOB_COMMANDS` + `mesh_a2a.schedules` `DEFAULT_INTERVALS` | 19, 22 | add one job entry each | trivial |
| `Makefile` (job targets) | 19, 22 | add one target each | trivial |
| `apps/pipeline/coordinator.py` | 19 (`synthesize` embed), 22 (`dispatch_investigations`), 23 (capture at dispatch) | **different nodes/functions** — keep edits local | low–medium |
| `mesh_llm/factory.py` + `__init__.py` | 20 only (additive) | new factory fn; existing untouched | none for others |
| `packages/mesh-models` / `mesh_db` new modules | each phase adds its own files | mostly new files, not shared edits | low |

The only one worth a word of care is **`coordinator.py`**: three phases touch it,
but in *different* nodes (19 in `synthesize`'s belief-write; 22 in
`dispatch_investigations`; 23 wrapping the skill-call sites). Keep each phase's
edits confined to its node and merges stay clean. Whoever merges first wins; the
rest rebase a localized hunk.

### Field switcher (Phase 18) is lightly foundational for 21 & 23 UI

Phase 18 adds a **field switcher in the nav**. Phases 21 and 23 both add
field-scoped wiki pages. Their specs say "respect Phase 18's field switcher if
present, else carry your own `?field=` selector" — so they're not blocked, but if
18 lands first the other two should consume its switcher rather than duplicate it.
Cheap to reconcile on merge.

---

## Per-workspace launch checklist

For each workspace, point the agent at its spec and the shared context:

1. **Branch** off `main` (Conductor does this per workspace).
2. **Read first** (every spec opens with a "Read before writing any code" list):
   the spec itself, this roadmap, and `CLAUDE.md`.
3. **Confirm the migration number** against the table above before writing any
   `migrations_pg/NNN_*.sql`.
4. **Execute blocks in order** (e.g. `19a → 19e`); tag per block; one logical
   commit per unit (Conventional Commits, per the repo policy).
5. **Gate each tag** on `uv run ruff check . && uv run mypy . && uv run pytest`
   (+ wiki `lint/typecheck/build` + Playwright for UI phases). CI runs ruff,
   mypy, pytest, the `api-types.ts` drift check, and Playwright on every push.
6. **Report principle conflicts** rather than working around them (each spec ends
   with this instruction).
7. **Do not push** unless asked (repo commit policy); open PRs against `main`.

### Suggested merge order (lowest-friction)

`20` (no migration, additive) → `18` (no migration; lands the field switcher) →
`19` → `21` → `22` → `23`. This lands the field switcher early for the UI phases
and applies migrations `011`→`014` in order. Any order works; this just minimizes
rebases.

---

## Invariants every phase inherits (do not violate)

From `CLAUDE.md` and the Phase 17 base — restated because all six must hold them:

- **Coordinator-owned writes.** Only the coordinator/job process writes, under
  `mesh_writer`. No agent role and no API path gains write (the API stays
  `mesh_reader`, except the existing Phase 9 schedule/scheduler surface).
- **Claims immutable; beliefs/heuristics/revisions append-only.** No new DELETE
  grant anywhere in these phases (19 and 23 call this out explicitly).
- **Field isolation is absolute.** `field_id` is a partition, never a content
  axis; resolution, memory, retrieval, consolidation, and discovery never cross
  fields. Default field `ai-robotics` reproduces today's behavior.
- **Engine logic doesn't move.** Synthesis dispatch, confidence math, curator
  scoring, the predicate/claim-type vocabulary, and merge semantics are fixed.
  Generalization happens at sources, prompts, scoping, and *new read/derived
  layers* — never by branching engine logic on a field or a model tier.
- **Graceful degradation.** Connector/fetch/LLM/observability failures record
  into `state["errors"]` (or a degraded API response) and never abort a run.
