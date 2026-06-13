# Phase 21 — Knowledge Chatbot: Grounded Q&A Over a Field's Knowledge Graph

## Context

Agent Mesh accumulates a structured, provenance-rich knowledge graph per field
(Phase 17): immutable `claims` extracted from `sources`, synthesized into mutable
`beliefs` (with confidence + `belief_signals`), anchored on resolved `entities`
and linked by `relationships`. The wiki exposes this as browsable lists + detail
pages + a force-directed graph. What it does **not** offer is the most natural
interface to a knowledge base: **asking it a question.** A user who wants "what's
the current state of <their field>?" or "is <entity> actually better than <other>
on <benchmark>, and how strong is the evidence?" has to navigate the graph by
hand.

This phase adds a **grounded retrieval-augmented Q&A** surface: the user asks a
natural-language question scoped to their field, the system retrieves the
relevant beliefs/claims/entities/relationships from *that field's* graph, and an
LLM synthesizes an answer **grounded strictly in the retrieved mesh content, with
inline citations** to the belief/claim/entity rows that support each statement.
It is field-agnostic by construction (it reads the same field-scoped tables every
other surface reads) and it is the system's first **read-only derived narrative
layer** — explicitly the "thematic synthesis" piece that Phase 19 deferred.

Because answers are LLM-generated over retrieved facts, **grounding and
citation are the whole game**: the agent answers *only* from the corpus, cites
every claim, and says "the mesh doesn't cover this" rather than inventing. The
existing `/api/v1/briefing` endpoint — which already dispatches an A2A agent
(the Personalizer) from the read-only API and returns a synthesized result — is
the precedent this phase follows for "API endpoint that calls an LLM over
field-scoped DB reads."

Read before writing any code — do not guess table, column, skill, or route
details:

- The knowledge models + access layer: `mesh_db.{beliefs,claims,entities,
  relationships}` (`list_beliefs`, `get_belief_signals`, `list_claims`,
  `list_entities`, the `Belief`/`Claim`/`Entity`/`Relationship` models), the
  `belief_signals` / `belief_hype_substance` views (migration 004), and the
  graph aggregation `mesh_db.graph` (`graph_nodes`, `graph_edges`).
- Field scoping (Phase 17): `field_id` on every knowledge table, the `?field=`
  query param on every `/api/v1/*` endpoint, `mesh_db.fields`
  (`get_field_by_slug`, `list_fields`), and `FieldProfile`.
- The briefing precedent: `apps/api/.../routers/briefing.py` (dispatches the
  Personalizer A2A skill from the read-only `mesh_reader` connection, caches by
  day + profile hash), `PersonalizerAgent` (`personalize_digest` skill), and how
  the API talks to A2A services.
- The LLM surface: `mesh_llm` (`make_llm_client` / `make_routed_llm_client` if
  Phase 20 landed, `LLMClient`, `messages.parse` structured output,
  `LLMResponseError`), and `mesh_agents.profiles.load_profile` for the field's
  framing.
- The wiki: nav (`apps/wiki/src/components/nav*.tsx`), data-fetch helpers
  (`apps/wiki/src/lib/api.ts`, `INTERNAL_API_URL` vs `NEXT_PUBLIC_API_URL`),
  shadcn primitives (`src/components/ui/*`), `make types`, and the
  detail-page routes citations will link to (`/knowledge/beliefs/[id]`,
  `/knowledge/claims/[id]`, `/knowledge/entities/[id]`).
- Playwright setup (`apps/wiki/playwright.config.ts`, the mock server, the page
  objects) for the new "Ask" page's coverage.

---

## Goal

A field-scoped **Ask** surface — a CLI command, an A2A `research_qa` skill, a
read-only API endpoint, and a wiki page — that answers natural-language questions
about a chosen field using only that field's knowledge graph, returns the answer
with **inline citations** to the supporting beliefs/claims/entities, surfaces a
**coverage/confidence** signal derived from the retrieved evidence, and refuses
to answer outside the corpus. No writes; no new role; the same field isolation as
every other surface.

---

## Principles (do not violate)

- **Grounded or silent.** The answer is synthesized strictly from retrieved mesh
  rows. The agent must not use parametric world knowledge to state field facts;
  when retrieval returns nothing relevant, it answers "the mesh has no evidence
  on this" — it does not fall back to its own training. This is the single most
  important property of the phase.
- **Every factual sentence is cited.** Each asserted fact carries a citation to
  the belief/claim/entity id(s) it came from. Uncited assertions are a bug. The
  structured output forces citations; the UI renders them as links to existing
  detail pages.
- **Read-only, role-respecting.** Retrieval runs on the `mesh_reader`
  connection. Answering calls an LLM but **writes nothing** — no conversation
  persistence, no belief mutation, no new write grant. (Multi-turn context, if
  any, is held client-side.)
- **Field isolation is absolute.** Retrieval filters every query by the
  requested field's `field_id`. A question in field B never retrieves field-A
  rows. Default field is `ai-robotics`, exactly like the rest of the API.
- **Confidence is the mesh's, not the model's.** Surface the retrieved evidence's
  own signals (belief `confidence`, `belief_signals`, source diversity, skeptic
  attacks) so the user sees how well-supported the answer is — never a number the
  LLM made up.
- **Retrieval is deterministic and explainable.** The context pack handed to the
  LLM is assembled by explicit, testable queries (FTS + structured traversal),
  not by an opaque step. The same question yields the same retrieved set.
- **No new hot-path coupling.** The Q&A path is request-time and isolated; it
  does not touch the pipeline, synthesis, or any agent's write path.

---

## Scope

### 1. Field-scoped retrieval surface — block 21a

Make the graph searchable; assemble grounded context packs.

- Migration `012_text_search.sql` (012 is the next free number after Phase 19's
  `011`; coordinate via the roadmap if numbering shifts): add Postgres full-text
  search support over the corpus — GIN `tsvector` indexes on
  `beliefs.statement` (+ `topic`), `claims.raw_excerpt`, and
  `entities.canonical_name` (+ `aliases`). Read-only optimization; `mesh_reader`
  already has SELECT, so **no grant change, no DELETE, no new write path.**
- `packages/mesh-db/src/mesh_db/search.py` (reader-safe): field-scoped FTS
  helpers — `search_beliefs(conn, query, *, field_id, limit)`,
  `search_claims(conn, query, *, field_id, limit)`,
  `search_entities(conn, query, *, field_id, limit)` — each returning rows ranked
  by `ts_rank` and filtered by `field_id`. Mirror the query style already used in
  `mesh_db` (parameterized SQL, `search_path` unqualified table refs).
- `gather_context(conn, question, *, field_id, budget)` — the retrieval
  orchestrator: FTS over beliefs/claims/entities for the question terms, then a
  bounded **structured expansion** (for the top entities/beliefs, pull their
  supporting/contradicting claims, signals, and one hop of relationships via the
  existing `mesh_db` readers and `graph_edges`). Returns a `ContextPack` — a
  citation-keyed, token-budgeted bundle of `{beliefs, claims, entities,
  relationships}` with stable citation ids. Document the budget knob
  (`MESH_QA_CONTEXT_BUDGET`) and what gets dropped when it's exceeded (`log` it).
- *(Optional, only if Phase 19 has merged its `beliefs.statement_embedding`)*:
  add a vector-similarity arm to `gather_context` reusing that column. Gate it
  behind a feature flag so this phase does **not** depend on Phase 19 — FTS +
  structured traversal is the required path; vectors are a bonus.

**Exit:** FTS indexes applied; `search_*` return field-scoped ranked rows;
`gather_context` assembles a citation-keyed, budget-bounded `ContextPack` for a
question and provably never includes another field's rows; unit-tested against
the testcontainer DB; `ruff` + `mypy --strict` clean. Tag `v0.21.0-phase-21a`.

### 2. Grounded answer agent + skill — block 21b

Turn a question + context pack into a cited answer.

- `packages/mesh-agents/src/mesh_agents/research_qa.py`: `ResearchQAAgent`
  (mirror an existing LLM agent's shape — input/output Pydantic models, `run`,
  an A2A `research_qa` skill). Input: `{question, field_id}`. It loads the
  field profile (`load_profile`) for framing, calls `gather_context`, builds a
  **grounding system prompt** (built from the `FieldProfile`, mirroring the
  Phase-17 prompt-builder pattern) that instructs: answer only from the provided
  context, cite every fact by its citation id, and explicitly say when the
  context is insufficient.
- Structured output (`messages.parse`): an `Answer` model with
  `answer_markdown`, a list of `Citation` (`{kind: belief|claim|entity, id,
  quote}`), a `coverage` enum (`well_supported | thin | uncovered`) derived from
  whether/which evidence was found and its signals, and `caveats`. Citations must
  reference ids that exist in the handed context pack — validate post-hoc and
  drop/flag any hallucinated id (never render a citation to a row that wasn't
  retrieved).
- Conservative on empty: if `gather_context` returns nothing above a relevance
  floor, short-circuit to `coverage="uncovered"` with a templated "no evidence in
  the mesh" answer — **without** calling the LLM (cheap + un-hallucinatable).
- Model selection via `make_routed_llm_client(agent_name="research_qa")` (falls
  back to `make_llm_client` if Phase 20 absent) — this is a synthesis task that
  benefits from the strong tier on hard questions.

**Exit:** given a question with supporting evidence, the agent returns a cited,
grounded answer whose every citation id is present in the retrieved context;
given an out-of-corpus question it returns `uncovered` without inventing facts;
unit-tested with a mock `LLMClient` + a stub context pack (including the
hallucinated-citation-drop path); `ruff` + `mypy --strict` clean. Tag
`v0.21.0-phase-21b`.

### 3. CLI + API — block 21c

Expose Q&A on the read paths.

- **CLI** (`apps/cli`, mirror `show-sota-beliefs` / `briefing` read commands):
  `mesh.cli ask "<question>" [--field <slug>]` — dispatches the `research_qa`
  skill (or runs the agent in-process against a reader connection) and renders
  the answer + citations as a `rich` panel with the cited ids.
- **API** (`apps/api`, mirror `routers/briefing.py`): `POST /api/v1/ask` body
  `{question}` + `?field=<slug>` → an `AskResponse` (`answer_markdown`,
  `citations[]`, `coverage`, `caveats`). It runs on the `mesh_reader` connection
  and dispatches the `research_qa` A2A skill exactly as briefing dispatches the
  Personalizer. CORS already allows POST from the wiki origin (Phase 9); confirm
  the method is permitted. Regenerate `make types`; CI drift check stays green.
  Document a request timeout + a graceful degraded response if the LLM/agent is
  unreachable (mirror briefing's degradation).

**Exit:** `mesh.cli ask` and `POST /api/v1/ask` both return a grounded, cited,
field-scoped answer; out-of-corpus questions return `uncovered`; the agent is
unreachable → a clean degraded response, not a 500; `make types` clean, no
OpenAPI drift; `ruff` + `mypy --strict` clean. Tag `v0.21.0-phase-21c`.

### 4. Wiki "Ask" page — block 21d

The user-facing chat surface.

- A new **Ask** nav entry (mirror the existing nav items; if Phase 18's field
  switcher has landed, the Ask page reads the selected field from it, otherwise
  it carries its own `?field=` selector). Client component built on shadcn
  primitives (`card`, `button`, `badge`, an input/textarea) — no new visual
  language.
- A question box → posts to `/api/v1/ask` (browser → `NEXT_PUBLIC_API_URL`) →
  renders `answer_markdown` with **citation chips** that link to the existing
  detail pages (`/knowledge/beliefs/[id]`, `/knowledge/claims/[id]`,
  `/knowledge/entities/[id]`), a coverage badge (well-supported / thin /
  uncovered), and any caveats. Loading + empty + error states like the other
  pages.
- Stateless single-turn for v1 (optional client-held follow-up context); **no
  conversation persistence.** Document the choice.
- Playwright: a page object + spec covering ask → answer-with-citations,
  citation links resolve, the uncovered state, and field scoping. Extend the mock
  server with an `/api/v1/ask` fixture.

**Exit:** a user asks a field-scoped question in the wiki, gets a cited answer
with working links to detail pages and an honest coverage badge, and an
out-of-corpus question shows the uncovered state; Playwright covers it; `ruff` +
`mypy --strict` + wiki lint/typecheck/build clean. Tag `v0.21.0-phase-21d`.

### 5. Docs — block 21e

Add `docs/knowledge-chatbot.md`: the retrieval pipeline (FTS + structured
expansion, the optional vector arm), the grounding/citation contract, the
coverage signal, field isolation, the read-only/no-persistence posture, and the
trust boundary (answers are only as good as the mesh's evidence). Match
`docs/personalization.md` / `docs/derived-signals.md` style. Update `CLAUDE.md`'s
phase-status paragraph + env-var table (`MESH_QA_*` knobs,
`MESH_LLM_MODEL_RESEARCH_QA`).

---

## Out of Scope (do not build)

- **Writing anything back into the knowledge base.** Q&A never creates claims,
  beliefs, or revisions. Answers are derived, ephemeral, read-only.
- **Conversation history / multi-turn memory persistence.** v1 is single-turn;
  any follow-up context is client-held. A persisted chat log + its write path is
  a later phase.
- **Agentic answering that triggers new ingestion** ("I don't know, let me go
  search"). Driving new investigation from a gap is **Phase 22 (Autonomous
  Discovery)**; this phase answers from what already exists.
- **Cross-field questions / federated answers.** One field per question.
- **A learned reranker, fine-tuning, or per-user personalization of answers.**
  Retrieval is deterministic FTS + traversal.
- **Voice, streaming token UI, or any new visual language** beyond shadcn
  primitives.

---

## Exit Criteria

- [ ] Migration `012` adds GIN `tsvector` indexes on beliefs/claims/entities;
      **no grant change, no write path, no DELETE**
- [ ] `mesh_db.search` FTS helpers + `gather_context` assemble a citation-keyed,
      budget-bounded, **field-scoped** `ContextPack`; provably never cross-field
- [ ] `ResearchQAAgent` / `research_qa` skill answer strictly from the context
      pack, cite every fact, drop hallucinated citation ids, and return
      `coverage` from the evidence; out-of-corpus → `uncovered` without an LLM
      call
- [ ] `mesh.cli ask` and `POST /api/v1/ask` return grounded cited field-scoped
      answers; degrade cleanly when the agent is down; `make types` clean, no
      drift
- [ ] Wiki **Ask** page renders the answer + working citation links + coverage
      badge, scoped to the selected field; Playwright covers it
- [ ] `docs/knowledge-chatbot.md` added; `CLAUDE.md` phase status + env table
      updated
- [ ] `ruff` + `mypy --strict` clean across touched packages; existing pytest +
      Playwright unaffected
- [ ] Read-only throughout; no new role; field isolation preserved; claims and
      beliefs unmodified

---

## Commit Convention

One logical commit per unit; conventional messages:

```
feat(db): add FTS indexes + field-scoped search helpers (migration 012)
feat(db): add gather_context citation-keyed retrieval orchestrator
feat(agents): add ResearchQAAgent + research_qa grounded-answer skill
feat(api,cli): add /api/v1/ask + mesh.cli ask
feat(wiki): add Ask page with cited answers + coverage badge
docs: add knowledge-chatbot.md; update CLAUDE.md
```

Tags map to blocks: `v0.21.0-phase-21a` (retrieval surface), `…-21b` (answer
agent), `…-21c` (CLI + API), `…-21d` (wiki Ask page), `…-21e` (docs). Execute
21a → 21e in order — retrieval is the foundation everything grounds on. Lint,
types, and a clean grounded-answer run are the bar before each tag. Report any
principle conflict (e.g. a retrieval path that can't stay field-scoped without an
engine change) before working around it.
