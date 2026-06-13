# Knowledge Chatbot

The Knowledge Chatbot answers natural-language questions about a field using
**only that field's knowledge graph**, with inline citations to the beliefs,
claims, and entities that support each statement. It is the system's first
read-only derived narrative layer — grounded retrieval-augmented Q&A over the
mesh. The wiki's `/ask` page, the `POST /api/v1/ask` endpoint, the `mesh.cli
ask` command, and the `research_qa` A2A skill are all surfaces over the same
agent.

The single most important property: **grounded or silent**. The agent answers
strictly from retrieved mesh rows; when retrieval finds nothing relevant it says
"the mesh has no evidence on this" rather than falling back to the model's own
training. Answers are only as good as the mesh's evidence — that is the trust
boundary.

## How it works

1. A surface dispatches the question + field to the ResearchQA agent (the API
   and skill go over A2A; the CLI runs the agent in-process on a reader
   connection).
2. The agent calls `mesh_db.gather_context(conn, question, field_id=…)`, which
   assembles a **context pack** by:
   - **Full-text search** over `beliefs(topic+statement)`,
     `claims(raw_excerpt)`, and `entities(canonical_name+aliases)` — GIN
     `tsvector` indexes (migration 011), OR-semantics, ranked by `ts_rank`,
     every query filtered by `field_id`.
   - **Structured expansion** from the top anchors: the supporting/contradicting
     claims and `belief_signals` for the top beliefs, and one hop of
     relationships (plus recent claims) for the top entities. Relationship
     endpoint entities are hydrated so edges are renderable.
   - **Budget trimming**: the claim set (the bulk) is trimmed to a character
     budget (`MESH_QA_CONTEXT_BUDGET`, default 12000), keeping the highest-ranked
     claims; the number dropped is logged.
3. If the pack is empty (nothing above the relevance floor), the agent
   short-circuits to a templated `uncovered` answer **without calling the LLM**
   — cheap and un-hallucinatable.
4. Otherwise the agent builds a grounding system prompt from the field's
   `FieldProfile` (`build_research_qa_system`) and a context block tagged with
   citation ids, and asks the LLM (via `make_routed_llm_client`) for a
   structured `Answer`.
5. The agent **validates citations**: any id the LLM emits that is not in the
   retrieved pack is dropped (never rendered as a link to a row that wasn't
   retrieved).
6. The agent sets **coverage** from the evidence, not the model: a model
   `uncovered` verdict (it judged the context irrelevant) or the absence of any
   valid citation → `uncovered`; otherwise `well_supported` when the evidence is
   reproduced / source-diverse or there are several corroborating claims, else
   `thin`.

Retrieval is deterministic and explainable: the same question yields the same
retrieved set. The Q&A path is request-time and isolated — it never touches the
pipeline, synthesis, or any agent's write path.

## The grounding & citation contract

- Every factual sentence carries at least one citation, written inline as
  `[belief:<id>]`, `[claim:<id>]`, or `[entity:<id>]`, using an id present in
  the context pack. Uncited assertions are a bug; the structured output forces
  citations and the agent drops hallucinated ids.
- The wiki renders inline citation tokens and a citation chip list as links to
  the existing detail pages (`/knowledge/{beliefs,claims,entities}/<id>`).
- `coverage` is the mesh's signal, never a number the model invented. It is
  derived from `belief_signals` (source-type diversity, reproduction count,
  skeptic attacks) and the size of the corroborating evidence.

## Field isolation

Every retrieval query filters by the requested field's `field_id`, and the
structured expansion only traverses from same-field anchors, so a question
scoped to field B can never surface field-A rows. The default field is
`ai-robotics`, exactly like the rest of the API. One field per question — there
are no cross-field or federated answers.

## Read-only, no persistence

Retrieval runs on the `mesh_reader` role. Answering calls an LLM but **writes
nothing**: no conversation log, no belief mutation, no new write grant. Migration
011 adds only indexes (no grant change, no DELETE, no write path). v1 is
single-turn and stateless; any follow-up context is held client-side. A persisted
chat log is a later phase, as is driving new ingestion from a gap (Phase 22,
Autonomous Discovery).

## Surfaces

| Surface | Entry point | Notes |
|---|---|---|
| Wiki | `/ask` page | Question box → cited answer + coverage badge + caveats |
| HTTP | `POST /api/v1/ask?field=<slug>` body `{question}` | Dispatches `research_qa`; degrades to a clean `uncovered` answer (200, not 500) when the agent is down; 504/502 on timeout/error |
| CLI | `mesh.cli ask "<question>" [--field <slug>]` | Runs the agent in-process; renders a `rich` panel + citations |
| A2A | `research_qa` skill (`research-qa` agent, :8016) | Input `{question, field_id}` → `Answer` |

## Configuration

See the env-var table in `CLAUDE.md`. The key knobs:

- `MESH_QA_CONTEXT_BUDGET` (default 12000) — character budget for the context
  pack; claims beyond it are dropped (logged).
- `MESH_QA_RELEVANCE_FLOOR` (default 1e-6) — minimum `ts_rank` for a row to
  enter the pack.
- `MESH_ASK_AGENT_URLS` / `MESH_ASK_TIMEOUT` — where the API reaches the agent,
  and its request timeout.
- `MESH_LLM_MODEL_RESEARCH_QA` (static pin) and routing knobs
  (`MESH_ROUTE_RESEARCH_QA_ENABLED`, `MESH_LLM_MODEL_RESEARCH_QA_STRONG`) — model
  selection. Grounded Q&A opts into tiered routing (Phase 20) so hard questions
  escalate to the strong tier; routing ships off by default.
