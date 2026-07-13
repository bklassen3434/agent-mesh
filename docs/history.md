# How Agent Mesh was built

The story of the project, in the order it happened — the decisions that stuck,
the ones that didn't, the bugs that taught us something, and what each move
actually bought. Roughly 300 commits across 24 phases and two architectural
pivots. It's told in eras, not commit-by-commit.

---

## The premise

The goal was never "a chatbot over some papers." It was a system that maintains
a *living* view of a research field: reads sources continuously, extracts facts,
reconciles them into a defensible position, challenges that position, and keeps
doing it without a person driving. Two ideas were load-bearing from day one and
never changed:

- **Claims are immutable; beliefs are mutable.** A claim is a fact pinned to a
  source and never edited — superseding evidence inserts a *new* claim. A belief
  is a synthesis over claims and changes over time, but every change writes an
  append-only revision. This split is the spine of the whole system; every later
  phase respects it.
- **Confidence is derived, not asserted.** No agent gets to declare "I'm 90% sure."
  Belief confidence is computed from evidence signals — source diversity,
  reproduction, how hard the skeptic hit it. That decision paid off much later
  when the skeptic and the discovery loop needed a number they could move.

---

## Era 1 — the substrate (Phases 0–7)

The first commits are unglamorous and deliberate: a `uv` workspace monorepo with
a strictly one-way dependency flow (`models ← db ← agents ← pipeline`), Pydantic
models for the seven core entities, a migration runner, and a DAL with one typed
module per entity. **Storage was DuckDB.** The CLI came before any agent —
you could inspect an empty knowledge store before anything filled it.

Then the agents landed one at a time: an arxiv scout, a claim extractor (the
first LLM call, structured output via Ollama running locally), an entity tracker
(case-insensitive find-or-create), a rule-based SOTA tracker. An orchestrator
strung them together with concurrency control and health checks.

**First bug worth remembering:** `datetime.utcnow()` everywhere, replaced
wholesale with `datetime.now(UTC)`. Naive timestamps are a slow-acting poison in
a system that reasons about staleness and decay — worth killing early.

**The A2A pivot (Phase 2).** Agents were promoted from in-process objects to
independent services speaking a structured agent-to-agent (A2A) JSON-RPC
protocol, each dispatching skills as async tasks. This was a real bet:
distribution and a wire contract, before there was any distribution problem to
solve. It made the fleet legible and independently restartable — and, years
later, most of it became orphaned scaffolding (see Era 6). Worth it anyway: the
skill-as-unit shape it forced survived every rewrite.

**The fleet fills out (Phases 4–7).** Scouts multiplied — Hacker News, GitHub
(trending + watchlist releases), Bluesky (public AppView), Reddit (OAuth2), blogs
(RSS/Atom), leaderboards (three failure-isolated lanes so one bad feed can't sink
the poll). Then the agents that *think* rather than fetch: the **Skeptic**, whose
entire job is to disagree with held beliefs and emit structured counter-claims
with a `failure_mode` taxonomy (`methodological_flaw`, `cherry_picked_evidence`,
…); the **Curator** for quality and staleness; the **Personalizer** for a daily
brief ranked against a markdown profile. Investigations became first-class — the
follow-up loop where a belief's gaps spawn directed searches.

**The read path (Phase 3).** A FastAPI service on :8000 (read-only) and a
Next.js 15 wiki on :3000. The rule that mattered: the wiki never touches the DB;
it consumes a typed JSON contract, and CI regenerates the TypeScript types from
the API's OpenAPI spec and fails on drift. The wiki is just the first client.

By the end of Era 1 the loop worked end-to-end on `localhost`, on a timer, with a
scheduler and a Pipelines control page in the wiki. It looked finished. It wasn't.

---

## Era 2 — making it real (Phases 8–12)

**LangGraph (Phase 8).** The hand-rolled orchestrator was replaced with LangGraph
`StateGraph`s — the coordinator and the skeptic sweep became stateful graphs with
conditional routing and `Send` fan-out, checkpointed to Postgres, one thread per
run. This bought crash-visibility and replay. (It, too, was later deleted — see
Era 6. The pattern outlived the library.)

**The cost era (Phase 11).** The first time real money showed up, the response was
to measure before optimizing. A `llm_usage` ledger recorded per-call tokens and
list-price cost, attributed in Langfuse. Baseline: extraction ran ~$0.15 for a
batch of papers. Three levers, each measured:

1. **Dedup before extraction.** A `processed_items` ledger skips already-seen
   sources. Verified with a *double run*: the second run over the same 57 papers
   re-extracted **0** and cost **$0.00**. In steady state you only pay for
   genuinely new material.
2. **Prompt caching, and a bug hiding as a non-feature.** The cache columns read
   `0/0` — caching never fired. The Anthropic client *was* marking the system
   prompt with `cache_control`; the prompt was simply below Haiku's 4,096-token
   minimum cacheable prefix. Expanding the extractor's few-shot prompt to ~4,767
   tokens crossed the threshold — after which 54/57 calls hit cache, billed at
   0.1×. The "expensive" fix (a longer prompt) was the cheap one.
3. **Batch API.** Belief evaluation moved to Anthropic's Message Batches for the
   discount, behind a flag.

The lesson that carried forward: **wire the ledger first.** Every later cost
decision (model routing, the Groq cheap tier, the daily budget brake) leaned on
having per-call numbers already in the store.

**The DuckDB → Postgres consolidation (Phase 12).** The system had drifted into
two stores — DuckDB for knowledge, Postgres for LangGraph checkpoints. That's an
operational tax and a consistency hazard. Everything moved onto a single pgvector
Postgres: a rewritten DAL on psycopg3, a one-time data-migration script, and a
hard split into a `mesh_writer` role (the pipeline) and a `mesh_reader` role (the
API) so read-only really is read-only, enforced by grants rather than convention.
DuckDB was deleted. This is the migration everything since has stood on.

---

## Era 3 — the knowledge gets smarter (Phases 13–16)

**Semantic entity resolution (Phase 13).** Exact-match dedup can't tell that
"GPT-4" and "GPT 4" are the same lab's model. Resolution became embedding-based:
block by pgvector similarity, then match in bands — high-similarity auto-merges,
low-similarity auto-rejects, and the ambiguous middle band goes to the LLM. A
merge re-points references and deletes the duplicate but **never touches claims**.
Conservative by design: a wrong merge is worse than a missed one.

**Synthesis beyond leaderboards (Phase 14).** Early synthesis assumed everything
was a score on a benchmark. Real fields aren't leaderboards. Every claim got a
`claim_type`, and synthesis split by type — SOTA scores, entity capabilities, and
claim-grounded graph edges each handled separately. Confidence moved fully to the
derived-signals formula with weights in config, not code.

**Memory (Phases 15–16).** Agents gained two kinds of memory: **episodic** (a
first-person read model of what an agent did and how it turned out, with
deterministic outcome tagging) and **procedural** (distilled `agent_heuristics`
that wired skills inject into their prompts). A consolidation pass turns episodes
into heuristics. This is where agents started to *learn* across runs rather than
starting cold every time.

---

## Era 4 — field-agnostic (Phases 17–18)

The biggest conceptual generalization. Everything up to here quietly assumed
"AI/robotics." Phase 17 made a **Field** a first-class entity: a `field_id` FK
scopes every row, entity resolution and memory never cross fields, and the three
coupled system prompts became profile-driven builders instead of hardcoded text.
`ai-robotics` became just the seeded default. Sources turned into a **connector
catalog** enabled per-field, with per-field config. The API and CLI took
`--field`/`?field=`. Nothing branches on the field name anywhere in the core —
that's the whole point, and it's verified by a field-isolation test that proves
no row references a row in another field.

---

## Era 5 — the mesh runs itself (Phases 19–24)

A run of features that made the system autonomous and inspectable:

- **Belief consolidation (Phase 19)** — beliefs de-dup by similarity like
  entities, but **strictly append-only**: a merged-away belief is marked
  not-held and keeps its revisions, never deleted. Stale beliefs decay on a
  half-life; dead ones archive. The invariant ("no belief or revision row is ever
  deleted") is enforced by a verification skill that snapshots before/after.
- **Tiered model routing (Phase 20)** — a `RoutedLLMClient` runs a cheap model by
  default and escalates to a strong one on a pure, LLM-free difficulty signal
  (long input) or a parse failure. Off by default; byte-for-byte the old behavior
  when off. A static model pin always wins.
- **Knowledge chatbot (Phase 21)** — a `research_qa` skill answers questions
  grounded in the store, with citations and a coverage badge. Later this became
  the front page.
- **Autonomous discovery (Phase 22)** — the inversion of the whole system. Instead
  of only *reacting* to incoming sources, the mesh analyzes its own field for gaps
  (under-evidenced entities, thin/stale beliefs, rising topics) and opens its own
  capped investigations. Crucially it proposes *evidence-gathering*, never facts.
- **Agent observability (Phase 23)** — an append-only `agent_invocations` row per
  skill dispatch (bounded input/output summaries, status, cost, injected memory),
  a `/agents` API, and a wiki page to click an agent and see what it was thinking.
- **Schema split (Phase 24)** — one Postgres, four schemas by concern
  (`knowledge`, `agents`, `runtime`, `catalog`), with a `search_path` spanning all
  so unqualified queries are unchanged.

---

## Era 6 — the two pivots (the agentic rewrite)

This is the most important part of the story, and the least visible from the
feature list.

**The problem.** By Phase 23 the system worked but was a *scheduled assembly
line*: fixed jobs (ingest, skeptic, discovery, consolidation) on fixed timers,
each a LangGraph pipeline. That's rigid. It can't notice that one belief suddenly
deserves attention while the rest of the field is quiet. It orchestrates *time*,
not *need*.

**The blackboard reframe.** The rewrite modeled the knowledge store as a
**blackboard**: the things that need attention are *derived* from the board's own
state as **tensions** (an unextracted source, a contested claim, an aging belief),
a controller picks them, and **skills** — the LLM unit, prompt + model + structured
output + injected memory — do the work by emitting typed **effects** that a single
**write gateway** applies under the invariants. Skills never write directly.

**The dead-end: the market.** The first selection layer was a *market*. Each skill
**bid** a value and a cost on each tension; a budget auction funded the best
value-per-dollar until the budget cleared. It was elegant on paper and it was
built — bids, an agenda auction, the works (`apps/pipeline/market.py`, a `Bid`
type). Then it was **ripped out and replaced with a deterministic rule engine**:
an ordered rule table mapping board state → activations. The blackboard, tensions,
skills, effects, and gateway all survived untouched; only the selection layer
changed, bidding → rules. Why: a deterministic rule table is debuggable,
testable, and predictable in a way an economic auction over LLM-estimated values
never is. When you can't trust the bids, the auction is just expensive
non-determinism. `market.py` and `Bid` are gone.

**Strangler-fig, so `main` stayed green.** The whole migration used two rules: the
shared shapes (`Tension`, `Effect`, `Skill`) were frozen on `main` *before* any
fan-out, and the controller grew *next to* the old coordinator as a new entry
point. Skills migrated in one at a time; only once the controller ran every step
end-to-end were the old LangGraph coordinator, skeptic sweep, discovery, and
consolidation jobs — and their console scripts — deleted. Parallel workspaces
each branched from a stable interface.

**Self-driving (Phase 35).** The last piece removed the scheduler entirely.
`mesh-controller --apply --forever` is its own driver: it runs the full pass to
quiescence, idles a backoff between empty passes, and re-senses. All cadence now
comes from the rules' own cooldowns (scouting, maintenance) plus that idle sleep —
no cron, no `schedules` table, no Pipelines page. The controller is the *sole*
orchestrator. Belief decay and memory consolidation fold in as cooldown-gated
rules, fired on a timer like scouting.

---

## Era 7 — production, and the bugs that came with it

Getting it live surfaced the class of bug you only see against the real world:

- **Empty claim objects churning synthesis.** Extraction occasionally emitted
  claims with empty objects; synthesis then re-fired forever on "unsynthesized"
  claims it could never satisfy. Fix: only emit fillable claims, and only
  synthesize the ones that can produce a belief. A self-driving loop is
  merciless about any rule that never reaches quiescence — it spins on it.
- **Merge-candidate tension churn.** A rejected entity merge came back as a fresh
  tension next round, re-paying the LLM every pass. Fix: durable rejections, so a
  "no" stays no.
- **arxiv rate-limiting.** Bursts got 429'd. Fix: a shared rate-limited client
  with configurable spacing and retries, plus throttling the discovery sweep and
  switching arxiv queries to keyword terms rather than the raw question.
- **Evidence-depth confidence.** A well-backed belief could rank below a one-off in
  a single-source-type field. Fix: a third support term (supporting-claim count),
  plus a backfill CLI to recompute confidence across all held beliefs — which then
  had to be taught to *page* all beliefs, not just the first page.
- **Raspberry Pi cold boot.** Healthchecks failed on slow arm64 startup; the fix
  was lengthening the `start_period`, not the check. Deployment realities, not
  logic bugs.

**Where it runs.** Live on a Raspberry Pi 5 (4GB) as an always-on daemon, reached
over Tailscale — the thesis being that production-readiness is about the loop
being durable and self-driving, not about a public URL. A Telegram bridge exposes
chat and the daily brief from a phone.

**The economics, closed out.** A Groq open-weight model became the cheap routing
tier with Haiku as the escalation target; rate-limited cheap-tier calls escalate
to the strong tier automatically. A **daily LLM budget brake** caps token and
dollar spend per UTC day — pin it to a provider's free daily quota and the
controller defers LLM-bound skills until the day rolls over, while LLM-free work
keeps running. The Phase 11 ledger is what made all of this measurable.

**Who gets to see what.** Admin vs. beta is a property of the running wiki
instance (`MESH_ADMIN_MODE`), never the browser — so a public visitor has no path
to admin at all, and the front page is a rate-limited anonymous chatbot.

---

## What the arc actually taught

- **Freeze the contract, then fan out.** Both the read path (typed API ↔ wiki)
  and the agentic rewrite (frozen `Tension`/`Effect`/`Skill`) worked because the
  interface was stable before the parallel work started.
- **Measure before optimizing.** The cost ledger came before every cost decision.
  The "expensive" fix (a longer prompt) was often the cheap one.
- **Determinism beats cleverness in the control layer.** The market was elegant
  and got deleted; a boring rule table is what actually runs. Save the LLM's
  judgment for the work, not the scheduling of the work.
- **Immutability up front is a gift to your future self.** Immutable claims and
  append-only revisions made entity merges, belief consolidation, and decay safe
  to build years later — nothing destructive to undo.
- **A self-driving loop punishes every non-quiescent rule.** The production bugs
  were mostly rules that never "finished." Autonomy raises the bar on correctness
  because there's no human to notice the spin.

For the current design (not the history), start with
[`architecture.md`](architecture.md) and
[`deterministic-controller.md`](deterministic-controller.md).
