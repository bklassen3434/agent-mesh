# Cost Baseline (Phase 11)

Living record of LLM token usage and estimated cost across the Phase 11
cost-reduction sub-phases. Each sub-phase appends its measured numbers so the
baseline → final comparison is auditable.

All costs are computed from the list-price table in
[`packages/mesh-llm/src/mesh_llm/pricing.py`](../packages/mesh-llm/src/mesh_llm/pricing.py)
(Anthropic list pricing, confirmed 2026-05-29). Token counts come from the
`llm_usage` ledger (migration 017), written per LLM call by the coordinator /
skeptic-sweep and read via `mesh.cli cost report --run-id <id>`.

## Method

- **Environment:** full docker fleet (`docker-compose.yml`), provider
  `anthropic`, against the live `mesh-data` DuckDB volume.
- **Model routing:** no `MESH_LLM_MODEL_*` overrides set — every agent runs the
  default **`claude-haiku-4-5`** ($1.00 / Mtok input, $5.00 / Mtok output).
  Per-agent routing is revisited in 11e.
- **LLM-backed skills:** only three skills call an LLM — `extract_claims`
  (claim extractor, main-pipeline fan-out), `challenge_belief` (skeptic, sweep
  fan-out), and `personalize_digest` (daily brief, outside the graphs). Scouts,
  entity-tracker, sota-tracker, and curator are rule-based.
- **Capture:** one full pipeline cycle and one full skeptic sweep, both run as
  one-shot containers (`docker compose --profile pipeline run --rm coordinator`
  / `--profile skeptic run --rm skeptic-sweep`).

---

## 11a — Baseline (no optimization)

Captured **2026-05-30** on `claude-haiku-4-5`.

### Pipeline run

| Field | Value |
|---|---|
| run_id | `eaa847f7-da0f-4777-bd62-8b9ca5cb7026` |
| run_type | pipeline |
| skill | `extract_claims` |
| calls | 72 |
| input tokens | 127,376 |
| output tokens | 4,042 |
| cache read / write tokens | 0 / 0 |
| **estimated cost** | **$0.1476** |

Per-call average: ~1,769 input tokens (stable system prompt + one abstract).
This run extracted 72 newly-scouted papers; many abstracts yielded no
qualifying claims (output ≈ 9 tokens — an empty claim list), which is expected
for the four narrow SOTA predicates.

> The run that produced these numbers predates the 11a idempotency fix and
> crashed on a duplicate-PK in `finalize` *after* writing all 72 ledger rows +
> the run row (data is complete and correct). The fix
> (`pipeline_run_exists` guard) is verified by a subsequent clean run
> `87443c23-cb37-4628-a217-7e142b4e549f` (exit 0).

### Skeptic sweep

| Field | Value |
|---|---|
| run_id | `8c4f673f-27ea-4d57-93d3-b664b03cab55` |
| run_type | skeptic_sweep |
| skill | `challenge_belief` |
| calls | 5 |
| input tokens | 13,184 |
| output tokens | 1,393 |
| cache read / write tokens | 0 / 0 |
| **estimated cost** | **$0.0201** |

Per-call average: ~2,637 input tokens (system prompt + belief + hydrated
supporting/contradicting claims). Five held beliefs were challenged.

> The live `mesh-data` volume held 0 synthesized beliefs at capture time, so 5
> representative SOTA beliefs were seeded from existing real claims (grouped by
> subject entity) to exercise the skeptic. The skeptic reasons over real
> hydrated claims regardless of how the belief was created, so the token
> volume is representative.

### Cache observation (the 11c target)

Both skills show **0 cache read / write tokens**. `AnthropicClient` already
marks the system prompt with `cache_control`, but the current system prompts
sit below the model's minimum cacheable prefix, so caching never fires. 11c
restructures the high-volume prompts (prefix-stable system + schema + few-shot
examples) to cross the threshold; the cache R/W columns above are the "before".

### Langfuse attribution (verified)

Generations in Langfuse carry per-call token usage and
`metadata = {agent, skill, cache_read_tokens, cache_creation_tokens}`, with the
trace named `<agent>:<skill>` — e.g. `extraction:extract_claims`,
`skeptic:challenge_belief`. Confirmed via `Langfuse().fetch_observations`.

---

## 11b — Deduplication before extraction

Captured **2026-05-30**. The coordinator now consults the `processed_items`
ledger (migration 018) before the extract fan-out. The ledger was backfilled
from the existing `sources` table on migration, so already-ingested items count
as processed from day one.

This lever is measured by a **double run**, not a single run's absolute cost —
per-run cost scales with how many *new* papers a scout happens to return.

| Run | Items seen | Items skipped | Extracted (`extract_claims` calls) | Cost |
|---|---|---|---|---|
| #1 `fbba00fa-6c21-44e0-90c7-b187895d4e49` | 57 | 56 | 1 | $0.0016 |
| #2 `2ae885e4-60e4-4973-8534-7c04f4f69c07` | 57 | 57 | **0** | **$0.0000** |

The second consecutive run made **zero** LLM extraction calls — every scouted
item was already in the ledger with an unchanged content hash. Skip counts are
visible in the `ingest` log line (`items_seen` / `items_skipped` /
`items_to_extract`) and in the run summary (`Items skipped`).

**Savings:** before dedup, a re-scout of the same ~57 items would re-extract all
of them (~$0.117 at the 11a per-paper rate). After dedup, that re-run costs
**$0**. In steady state the pipeline only pays to extract genuinely new or
content-changed items.

> Scope: exact + content-hash dedup keyed on `(source_type, url)`. Semantic
> near-duplicate detection (reworded titles, cross-posts) via `duckdb-vss` is a
> deliberate follow-on, not built here.

---

## 11c — Prompt caching

Captured **2026-05-30** on `claude-haiku-4-5`.

**Finding (verified at docs.claude.com):** Claude Haiku 4.5's minimum cacheable
prefix is **4,096 tokens** (vs 1,024 for Sonnet 4.6). Cache write = 1.25x,
read = 0.1x, 5-min TTL, max 4 breakpoints. The `AnthropicClient` already marks
the system prompt with `cache_control`; the prompts were simply below the
threshold (extractor ~1,062 tok, skeptic ~839 tok), so caching never fired —
the 11a 0/0 cache columns.

**Change:** the claim-extractor system prompt was expanded to **~4,767 tokens**
with diverse, correct few-shot examples (all four predicates; blog / leaderboard
/ forum / repo / robotics sources; empty-list and marketing-only cases). This
crosses the threshold so caching fires, and — bonus — markedly improves
extraction (see below). The prompt stays fully prefix-stable; the variable
abstract remains in the user message. The skeptic was intentionally left alone
(~5 calls/sweep doesn't amortize a forced 4k prefix).

### Verification run (fresh DB, 57 papers extracted)

run_id `b50d8d2d-df28-4c40-a92e-2a0cafabea38`:

| Metric | Value |
|---|---|
| `extract_claims` calls | 57 |
| calls with a **cache read** | **54 / 57** |
| cache-read tokens (billed 0.1x) | 283,986 |
| cache-write tokens (billed 1.25x) | 15,777 |
| uncached input tokens (abstracts) | 12,035 |
| output tokens | 2,783 |
| **cost** | **$0.0741** |
| claims extracted | **35** (vs ~0 on most baseline papers) |

Langfuse generations show `cache_read` tokens (~5,259/call — system + tool
schema) consistent across consecutive calls, confirming the cache hits and that
no variable content sits in the cached prefix.

**Effect:**
- **Per-call cost:** $0.0741 / 57 = **$0.0013/call**, down ~37% from the 11a
  baseline's **$0.00205/call** — *despite* the prompt being 4.7x larger,
  because the prefix is cached at 0.1x.
- **Without caching**, sending the same 4.8k prompt uncached would cost
  ~$0.33 for this run; caching brought it to **$0.074 (~77% less)**.
- **Quality:** the richer few-shot prompt lifted extraction from ~0 claims on
  most papers to 35 claims across 20 entities in this run.
- `OllamaClient` is unaffected — it never emits cache markers; the larger
  system prompt is just more text, and its tests pass.

---

## Progression

Estimated cost per workload, to be filled in as each sub-phase lands.

| Stage | Pipeline run | Skeptic sweep | Notes |
|---|---|---|---|
| 11a baseline | **$0.1476** (72 calls) | **$0.0201** (5 calls) | Haiku 4.5, no cache, no dedup |
| 11b post-dedup | **$0.00** on re-run (0 calls) | n/a (sweep has no scouting) | unchanged items skipped; only new/changed extracted |
| 11c post-caching | **$0.0013/call** (was $0.00205, 54/57 cache reads) | unchanged (skeptic not cached) | 4.8k cached prefix at 0.1x; cheaper/call + better extraction |
| 11d post-batch sweep | _TBD_ | _TBD_ | Batch API (~50% off) for sweep |
| 11e final routing | _TBD_ | _TBD_ | per-agent model tier audit |

> Note: per-run pipeline cost scales with the number of *newly extracted*
> papers. The 11b dedup lever is measured by re-running the pipeline twice and
> showing near-zero `extract_claims` calls on the second run, not by comparing a
> single run's absolute cost.
