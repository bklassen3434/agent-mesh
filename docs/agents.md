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

Concurrency cap is `MESH_PIPELINE_CONCURRENCY` (default 3). One bad paper records an error and continues; Ollama connection failure aborts.
