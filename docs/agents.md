# Agents

## Overview

The agent fleet started (Phase 1) as plain Python classes, each with a single `async run()` method over Pydantic input/output models. **Every agent is now an A2A server** (Phase 2 onward): each subclasses `BaseAgent`, advertises one or more skills (`skill_id`), and is dispatched by the LangGraph coordinator over the A2A protocol via `mesh_a2a.node.call_skill_node`. Core agent logic is unchanged from the original classes; the transport and orchestration moved out into `apps/pipeline/` and `mesh-a2a`.

Cross-cutting properties of the current fleet:

- **Field-agnostic (Phase 17).** All agents operate within a **Field** (`knowledge.fields`). A run scopes to one field and the coordinator dispatches only that field's enabled connectors. The three coupled system prompts (extractor, skeptic, research-QA) are profile-driven **builders** (`mesh_llm.prompts.build_*` from a `FieldProfile`), and agents build the `cache_control` prefix once per field via `mesh_agents.profiles.load_profile`. Entity resolution and memory **never cross fields**. `field_id` is a partition, never a content axis — synthesis/confidence/curator logic never branches on it. See `docs/field-agnostic.md`.
- **Observable (Phase 23).** Every agent dispatched through the standard skill path is recorded: a `_dispatch` wrapper in the coordinator times each skill call and writes an `AgentInvocation` (bounded input/output summary, status, trace id, latency, model/tokens/cost, injected memory) to `knowledge.agent_invocations`. Rows surface on the wiki **Agents** page (roster → an agent's recent invocations → one invocation's inputs/outputs/context + Langfuse deep-link). No per-agent code is needed for an agent to appear. See `docs/agent-observability.md`.
- **Memory.** Agents can inject episodic recall + learned heuristics and attach an optional debug envelope to skill output (`mesh_agents.memory`). See `docs/agent-memory.md`.

## Agent Catalogue

### ArxivScoutAgent

**Responsibility**: Fetch recent arxiv papers in given categories.

**Input**: `ArxivScoutInput(categories, max_results, since)`  
**Output**: `ArxivScoutOutput(papers: list[ScoutedPaper])`

Queries the arxiv API, sorts by submission date descending, filters by `since` date, and wraps each result in a `ScoutedPaper` containing the pre-built `Source` model (with `raw_content_hash = sha256(abstract)`). No LLM calls.

**Skill**: `scout_arxiv` (port `8001`). Also serves the `investigate_arxiv` skill used by the investigation/discovery path.

---

### ClaimExtractorAgent

**Responsibility**: Extract structured claims from a source's content using the configured LLM.

**Input**: `ClaimExtractorInput(paper: ScoutedPaper)`  
**Output**: `ClaimExtractorOutput(claims, entities_referenced, latency_ms)`

Calls the configured `LLMClient` with structured output (`ClaimExtractionResult` schema) under a field-profile-driven, cache-controlled system prompt. Allowed predicates: `achieves_score`, `outperforms`, `developed_by`, `evaluated_on`, plus the five Phase 14 predicates `has_capability`, `based_on`, `reproduces`, `critiques`, `speculates`. Each predicate maps 1:1 to a `claim_type` consumed by synthesis.

Opts into tiered model routing via `make_routed_llm_client(agent_name="claim_extractor")` (Phase 20) and ships the Phase 23 memory debug envelope.

On LLM parse failure: logs and returns empty list (pipeline continues).  
On provider connection failure: re-raises (pipeline must abort).

**Skill**: `extract_claims` (port `8002`).

---

### EntityTrackerAgent

**Responsibility**: Resolve entity names to IDs, creating new entities as needed — now via **semantic entity resolution** (Phase 13), not just exact match.

**Input**: `EntityTrackerInput(names, type_hints)`  
**Output**: `EntityTrackerOutput(resolved: dict[str, str], created_count)`

**Skill**: `resolve_entities` (port `8003`).

Resolution is **block → match → merge** on `name_embedding` similarity (pgvector, HNSW cosine; embedder `fastembed`/`BAAI/bge-small-en-v1.5` via the `Embedder` protocol). An alias/canonical exact-match fast-path still wins first; otherwise the coordinator runs `resolve_entity_semantic` (`mesh_agents.entity_resolution`) before creating any new entity: type-filtered blocking finds candidates, then conservative bands decide — cosine ≥ `MESH_ENTITY_MERGE_HIGH` (0.93) auto-merges, ≤ `MESH_ENTITY_MERGE_LOW` (0.80) auto-rejects, and the middle band is adjudicated by the LLM (defaulting to not-same). Blocking and the name fast-path **never cross fields**.

A transactional `merge_entities` re-points claim/relationship/investigation references B→A, aggregates colliding edges, folds aliases, and deletes B — never touching claim content. The one-time backfill is `mesh.cli reconcile-entities` (Batch API). The coordinator's adjudication call opts into tiered routing. See `docs/entity-resolution.md`.

**DB exception**: This agent reads and writes the DB directly (find-or-create pattern).

---

### SotaTrackerAgent

**Responsibility**: Rule-based synthesis of `achieves_score` claims into SOTA beliefs.

**Input**: `SotaTrackerInput(claims_with_resolved_entities: list[ResolvedClaim])`  
**Output**: `SotaTrackerOutput(belief_updates: list[BeliefUpdate])`

**Skill**: `update_sota` (port `8004`).

Groups `achieves_score` claims by `object["benchmark"]`. For each benchmark:
- No existing belief → `BeliefUpdate(is_new_belief=True)`
- New score beats existing → `BeliefUpdate(is_new_belief=False, existing_belief_id=...)`
- New score ≤ existing → no update

No LLM calls. This is one branch of a **generalized synthesis** layer (Phase 14): the coordinator's `synthesize` node dispatches on each claim's `claim_type` (`mesh_agents.synthesis`):

- `score` → this SOTA handler (unchanged).
- `capability` → entity-anchored beliefs keyed `capability:<entity_id>` that converge per canonical entity.
- relational types (`comparison`/`attribution`/`lineage`/`evaluation`) → claim-grounded edges in the `relationships` table (so `/graph` has edges), mapping to `outperforms`/`developed_by`/`based_on`/`evaluated_on`.

Belief confidence is no longer a hardcoded `0.5` — `mesh_agents.confidence.compute_confidence` derives it from the `belief_signals` view (source diversity, reproduction, skeptic attacks) with config-tunable `MESH_CONFIDENCE_*` weights. See `docs/belief-synthesis.md`.

---

### HNScoutAgent (Phase 4)

**Responsibility**: Fetch AI/robotics-relevant Hacker News stories via Algolia.

**Input**: `HNScoutInput(keywords, max_results, min_points)`  
**Output**: `HNScoutOutput(papers: list[ScoutedPaper])` — same shape as ArxivScout, so the downstream pipeline consumes HN sources unchanged.

**Skill**: `scout_hn` (port `8005`). Coordinator dispatches via the `scout_*` prefix loop — no per-source branching required.

---

### SkepticAgent (Phase 4)

**Responsibility**: Challenge an existing belief by finding evidence problems and emitting counter-claims.

**Input**: `SkepticInput(belief, supporting_claims, contradicting_claims, in_scope_entities)`  
**Output**: `SkepticAssessment(verdict, confidence, rationale, suggested_confidence_delta, counter_claims)`

Verdicts: `supported` | `weakened` | `contradicted` | `inconclusive`. The skeptic constrains `subject_entity_id` on every counter-claim to the caller-supplied `in_scope_entities` set; out-of-scope refs are defensively dropped.

**LLM**: `make_llm_client(agent_name="skeptic")` — overridable via `MESH_LLM_MODEL_SKEPTIC`.

**Exception handling**: `LLMProviderNotReadyError` is fatal at startup; `LLMResponseError` during an assessment collapses to an `inconclusive` sentinel so the sweep does not abort.

**Skill**: `challenge_belief` (port `8006`). Activated only under the `skeptic` docker profile.

---

### CuratorAgent (Phase 4)

**Responsibility**: Rank held beliefs by how worth-challenging they are; return the top-N for the Skeptic to assess.

**Input**: `CuratorInput(beliefs, pick_count, now, cooldown_days)`  
**Output**: `CuratorOutput(picks: list[CuratorPick])` — sorted by score descending.

Pure / rule-based — no LLM. The score weights staleness, supporter weakness, confidence extremity, a flat boost for recent contradicting activity, and a cooldown penalty for beliefs the skeptic just looked at (caller derives `last_challenged_at` from `belief_revisions WHERE revised_by_agent='skeptic'`).

**Skill**: `select_beliefs_to_challenge` (port `8007`). Activated only under the `skeptic` docker profile.

---

## Orchestrator Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                      run_pipeline()                             │
│                                                                 │
│  1. OllamaClient.health_check()  ←── fail fast                 │
│                                                                 │
│  2. ArxivScoutAgent.run()                                       │
│       └─ list[ScoutedPaper]                                     │
│                                                                 │
│  3. Filter by raw_content_hash ←── dedup against DB            │
│                                                                 │
│  4. Insert new Sources to DB                                    │
│                                                                 │
│  5. ClaimExtractorAgent.run()  × N  (asyncio.Semaphore(3))      │
│       └─ list[ExtractedClaim] per paper                        │
│                                                                 │
│  6. EntityTrackerAgent.run()   (all names at once)              │
│       └─ dict[name → entity_id]                                 │
│                                                                 │
│  7. Resolve claims + insert Claim rows to DB                    │
│                                                                 │
│  8. SotaTrackerAgent.run()                                      │
│       └─ list[BeliefUpdate]                                     │
│                                                                 │
│  9. Persist BeliefUpdates (create or revise)                    │
│                                                                 │
│ 10. Write PipelineRun record                                    │
└─────────────────────────────────────────────────────────────────┘
```

Concurrency cap is `MESH_PIPELINE_CONCURRENCY` (default 3). One bad paper records an error and continues; LLM provider failure aborts.

## Falsification Sweep (Phase 4)

Out-of-band from the main pipeline. Triggered manually by `make skeptic`.

```
┌─────────────────────────────────────────────────────────────────┐
│                  run_skeptic_sweep()                            │
│                                                                 │
│  1. Discover Curator + Skeptic via MESH_SKEPTIC_AGENT_URLS      │
│                                                                 │
│  2. Read all currently_held beliefs                             │
│                                                                 │
│  3. Per belief, derive last_challenged_at + recent contradict   │
│     activity from belief_revisions                              │
│                                                                 │
│  4. call_skill("select_beliefs_to_challenge", {beliefs, ...})   │
│       └─ Curator returns top-N CuratorPicks                     │
│                                                                 │
│  5. For each pick:                                              │
│       a. Hydrate supporting/contradicting claims                │
│       b. Build in_scope_entities from claim subject ids         │
│       c. call_skill("challenge_belief", {belief, claims, ...})  │
│       d. If verdict ∈ {weakened, contradicted}                  │
│          AND confidence ≥ MESH_SKEPTIC_APPLY_THRESHOLD:         │
│            - Insert one Source (type=agent_reasoning)           │
│            - Insert counter-claims (extracted_by_agent=skeptic) │
│            - Update belief (confidence delta; for contradicted, │
│              extend contradicting_claim_ids)                    │
│            - Append BeliefRevision (revised_by_agent=skeptic)   │
│                                                                 │
│  6. Write PipelineRun row with run_type='skeptic_sweep'         │
└─────────────────────────────────────────────────────────────────┘
```

Coordinator (`apps/pipeline/coordinator.py`) is **unchanged** by this flow. Curator never calls Skeptic directly — `skeptic_sweep` brokers everything via A2A so the agent boundary stays load-bearing.

---

## Phase 5b — New scouts (complete)

Phase 5b adds four new scout agents. All drop in via the existing
`scout_*` skill-id prefix dispatch in the coordinator — no per-source
branching was added.

### GitHubScoutAgent (Phase 5b)

**Responsibility**: Surface high-signal ML/AI work from GitHub.

**Skill**: `scout_github` (port `8008`).

Two fetch lanes, both unauthenticated by default:

1. **Trending**: GitHub search API (`/search/repositories`) filtered by
   `topic:` clauses (default: `llm`, `agents`, `machine-learning`,
   `ai`, `robotics`), sorted by stars. For each repo the scout
   best-effort fetches the README and uses it as the abstract; falls
   back to description + topics list when the README is missing.
2. **Watchlist**: per-repo `/releases.atom` feed for every `owner/repo`
   slug in `MESH_GITHUB_WATCHLIST`. Release notes become the abstract.

Env: `MESH_GITHUB_TOPICS`, `MESH_GITHUB_WATCHLIST`, `GITHUB_TOKEN`
(optional; PAT raises the public 60/hr limit to 5000/hr).

---

### BlueskyScoutAgent (Phase 5b)

**Responsibility**: Fetch AI/ML posts from Bluesky's public AppView API.

**Skill**: `scout_bluesky` (port `8009`).

Two lanes, both via the unauthenticated AppView at
`public.api.bsky.app`:

1. **Hashtag**: `app.bsky.feed.searchPosts` for each tag in
   `MESH_BLUESKY_HASHTAGS` (default: `ai`, `ml`, `llm`).
2. **Author**: `app.bsky.feed.getAuthorFeed` per handle in
   `MESH_BLUESKY_HANDLES` (optional curated list).

Posts shorter than ~40 chars get filtered to keep the LLM extraction
budget on signal.

---

### RedditScoutAgent (Phase 5b)

**Responsibility**: Top posts of the day from AI/ML subreddits.

**Skill**: `scout_reddit` (port `8010`).

Uses Reddit's free OAuth2 client-credentials grant — requires
`REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` (create a "script"-type
app at <https://www.reddit.com/prefs/apps>). Without creds the scout
returns empty and logs a single warning; the rest of the pipeline keeps
running.

Default subreddits: `MachineLearning`, `LocalLLaMA`, `singularity`,
`artificial` (override via `MESH_REDDIT_SUBS`). Min-score filter
defaults to 20 to drop fly-by posts.

---

### BlogScoutAgent (Phase 5b)

**Responsibility**: Ingest AI/ML blog posts via RSS/Atom feeds.

**Skill**: `scout_blogs` (port `8011`).

Reads feeds from `config/blog_feeds.yaml` (default), or overrides via:

- `MESH_BLOG_FEEDS` — comma-separated URLs
- `MESH_BLOG_FEEDS_FILE` — path to an alternate YAML file

Default feed list (curated for Phase 5b): Anthropic, OpenAI, Google
DeepMind, Meta AI, Hugging Face, Simon Willison, Lilian Weng, Sebastian
Raschka, Berkeley BAIR, Stanford AI Lab.

Lookback window `MESH_BLOG_LOOKBACK_HOURS` (default 24) skips entries
older than the window so each run only ingests genuinely new posts.

Uses `feedparser`, which is robust to malformed feeds — a broken or
empty feed gets one warning and is skipped.

---

### LeaderboardScoutAgent (Phase 5b)

**Responsibility**: Snapshot top entries from evaluation surfaces.

**Skill**: `scout_leaderboards` (port `8012`).

Three failure-isolated sub-fetchers; one breaking does not affect the
others:

1. **HuggingFace Open LLM Leaderboard** — via the public
   `datasets-server.huggingface.co/rows` endpoint against the
   `open-llm-leaderboard/contents` dataset.
2. **Papers-with-Code SOTA** — `/api/v1/sota/<benchmark>/` for a small
   set of foundational benchmarks (`mmlu`, `humaneval`, `gsm8k`,
   `hellaswag`, `arc`).
3. **Chatbot Arena (LMSys)** — the public CSV at
   `huggingface.co/spaces/lmsys/chatbot-arena-leaderboard/.../leaderboard_table.csv`.

Each lane produces one ScoutedPaper whose abstract lists the top-N
entries in the format `"<rank>. <model> — <metric> on <benchmark>"`,
which the claim extractor parses into structured `achieves_score`
claims.

The exact public endpoints these sub-fetchers hit can change. When one
breaks, the agent stays up and the other lanes keep working — the user
can iterate on the broken lane without redeploying everything.

---

### PersonalizerAgent (Phase 7)

**Responsibility**: Rank and frame the day's belief activity into a personalized Daily Brief.

**Skill**: `personalize_digest` (port `8013`).

LLM-backed digest agent behind the wiki's Daily Brief. Overridable model via the standard per-agent env. See `docs/personalization.md`.

---

## Connector-driven scouts (Phase 17/18)

These three generic scouts are the connector catalog's universal fetchers: they take their endpoint/config from the field's enabled connector (`knowledge.field_connectors`) rather than hardcoding sources, so a new field can ingest data with no new agent code.

### WebSearchScoutAgent

**Responsibility**: Brave web search, config-driven.

**Skill**: `scout_web_search` (port `8017`); emits `Source(type=web)`. Also serves `investigate_web` — the universal fallback for the investigation/discovery path.

### RssScoutAgent

**Responsibility**: Generic RSS/Atom feed ingestion, config-driven.

**Skill**: `scout_rss` (port `8014`); emits `Source(type=rss)`.

### RestJsonScoutAgent

**Responsibility**: Generic REST/JSON endpoint ingestion, config-driven.

**Skill**: `scout_rest_json` (port `8015`); emits `Source(type=rest)`.

---

## ResearchQAAgent (Phase 21)

**Responsibility**: The knowledge chatbot backing the wiki `/ask` page — cited, store-grounded answers.

**Skill**: `research_qa` (port `8016`; wired via `MESH_ASK_AGENT_URLS`).

A deterministic, field-scoped context pack is assembled from the knowledge store (`mesh_db.gather_context`), then a single LLM pass (`build_research_qa_system` profile prompt) synthesizes a grounded `Answer` with `Citation`s and a `Coverage` verdict. Citations whose id was not in the retrieved pack are dropped, so every claim in the answer traces back to stored evidence; no-evidence questions return `uncovered`. See `docs/knowledge-chatbot.md`.

---

## Synthesis-side / out-of-band agents

These run on their own LangGraph jobs and schedules rather than inline in the main pipeline.

### discovery analyzer (Phase 22)

**Responsibility**: Proactively propose investigations from whole-field gaps — never proposes facts.

`mesh_agents.discovery.analyze_field` is a rule-based pass that mines under-evidenced entities, thin/stale beliefs, rising-activity topics, and missing reciprocal edges into ranked `GapSignal`s; one LLM pass (`draft_hypotheses`, `make_routed_llm_client(agent_name="discovery")`, field-framed via `build_discovery_system`) turns them into testable proposals, degrading to `[]` on failure and deduping against open investigations. The `mesh-discover` job opens capped `origin="discovery"` investigations per active field and dispatches real search via `dispatch_open_investigations` (only to connectors enabled for the field). Fired by a daily scheduler job; `mesh.cli discover [--field --apply]` is the dry-run/manual entry. See `docs/autonomous-discovery.md`.

### belief_consolidator (Phase 19)

**Responsibility**: Append-only belief de-duplication + decay/archive — the world-model analog of entity resolution, but it never deletes.

Beliefs carry a `statement_embedding`; `mesh_agents.belief_consolidation`/`belief_reconcile` block held, field-scoped, family-restricted candidate beliefs, then merge on conservative bands (`MESH_BELIEF_MERGE_HIGH`/`_LOW` 0.95/0.85; middle band → LLM, defaulting to not-same). `merge_beliefs` folds claim-id unions, recomputes confidence, re-points investigation refs, and marks the duplicate not-held — never deleting a row or touching a claim. A second LLM-free pass decays stale beliefs (confidence half-life) and archives long-dead unsupported ones. Every change appends a `BeliefRevision` attributed to `belief_consolidator`, never crossing fields. Runs as the daily `mesh-belief-consolidate` job; `mesh.cli consolidate-beliefs` is the backfill/manual entry. See `docs/belief-consolidation.md`.
