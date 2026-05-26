# Agents

## Overview

Phase 1 introduces four agents as plain Python classes. Each has a single `async run()` method with Pydantic input/output models. The orchestrator drives them sequentially; no agent schedules itself or reads from a queue.

Phase 2 will promote these to A2A servers by subclassing `BaseAgent` with an A2A transport layer. Agent logic stays unchanged.

## Agent Catalogue

### ArxivScoutAgent

**Responsibility**: Fetch recent arxiv papers in given categories.

**Input**: `ArxivScoutInput(categories, max_results, since)`  
**Output**: `ArxivScoutOutput(papers: list[ScoutedPaper])`

Queries the arxiv API, sorts by submission date descending, filters by `since` date, and wraps each result in a `ScoutedPaper` containing the pre-built `Source` model (with `raw_content_hash = sha256(abstract)`). No LLM calls.

**Phase 2 note**: Becomes a Scout A2A server that emits `NewSource` events.

---

### ClaimExtractorAgent

**Responsibility**: Extract structured claims from a paper abstract using a local LLM.

**Input**: `ClaimExtractorInput(paper: ScoutedPaper)`  
**Output**: `ClaimExtractorOutput(claims, entities_referenced, latency_ms)`

Calls `OllamaClient.complete_with_latency()` with structured output (`ClaimExtractionResult` schema). Allowed predicates: `achieves_score`, `outperforms`, `developed_by`, `evaluated_on`.

On LLM parse failure: logs and returns empty list (pipeline continues).  
On Ollama connection failure: re-raises (pipeline must abort).

**Phase 2 note**: Becomes an Extractor A2A server; subscribes to `NewSource` events.

---

### EntityTrackerAgent

**Responsibility**: Resolve entity names to IDs, creating new entities as needed.

**Input**: `EntityTrackerInput(names, type_hints)`  
**Output**: `EntityTrackerOutput(resolved: dict[str, str], created_count)`

Case-insensitive exact match against `canonical_name` and `aliases`. Embedding-based fuzzy resolution is deferred to Phase 2.

**DB exception**: This agent reads and writes the DB directly (find-or-create pattern).

**Phase 2 note**: Becomes a Curator A2A server with deduplication and merge capabilities.

---

### SotaTrackerAgent

**Responsibility**: Rule-based synthesis of `achieves_score` claims into SOTA beliefs.

**Input**: `SotaTrackerInput(claims_with_resolved_entities: list[ResolvedClaim])`  
**Output**: `SotaTrackerOutput(belief_updates: list[BeliefUpdate])`

Groups `achieves_score` claims by `object["benchmark"]`. For each benchmark:
- No existing belief → `BeliefUpdate(is_new_belief=True)`
- New score beats existing → `BeliefUpdate(is_new_belief=False, existing_belief_id=...)`
- New score ≤ existing → no update

No LLM calls. Phase 4 introduces the Skeptic agent for nuanced confidence calibration.

**Phase 2 note**: Becomes a Synthesizer A2A server; subscribes to `NewClaim` events.

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
