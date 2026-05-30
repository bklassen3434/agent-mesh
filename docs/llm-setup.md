# LLM Setup

Agent Mesh supports two LLM providers, selectable via `MESH_LLM_PROVIDER`:

| Provider | Default model | Cost | Setup | When to use |
|---|---|---|---|---|
| `anthropic` (default) | `claude-haiku-4-5` | ~$0.01–$0.05 per pipeline run | API key | Best quality, fastest path; needs internet + a key |
| `ollama` | `qwen3:8b` | Free (local GPU/CPU) | Install Ollama, pull a model | Offline / no API key / large-volume testing |

Both paths use the same `LLMClient` Protocol; the pipeline picks one at startup based on `MESH_LLM_PROVIDER` via `make_llm_client()`.

---

## Anthropic (default)

### Get a key

1. Sign up at <https://console.anthropic.com>.
2. **Billing** → add a payment method and **set a monthly spend cap** (e.g. $5). Even sloppy testing on Haiku 4.5 stays well under this.
3. **API Keys** → "Create Key", copy the `sk-ant-...` value (shown once).

### Configure

In `.env`:

```
MESH_LLM_PROVIDER=anthropic
MESH_LLM_MODEL=claude-haiku-4-5
ANTHROPIC_API_KEY=sk-ant-...
```

`MESH_LLM_MODEL` accepts any current Claude model ID — `claude-haiku-4-5`, `claude-sonnet-4-6`, `claude-opus-4-7`. The aliases auto-track the latest snapshot; you can also pin a dated ID like `claude-haiku-4-5-20251001`.

### Cost (Haiku 4.5)

Pricing per million tokens: **$1.00 input, $5.00 output**.

A typical `make pipeline` run (20 arxiv papers, ~500 input + ~500 output tokens each) costs **roughly $0.01–$0.05**. Set a low monthly cap in the console for peace of mind.

### Prompt caching (Phase 11c)

The claim-extractor system prompt is sent with `cache_control: {"type": "ephemeral"}`, so the stable prefix is reused across calls within a 5-minute window at a 0.1× read rate (write is 1.25×).

Haiku 4.5 has a **4,096-token minimum cacheable prefix**. As of Phase 11c the claim-extraction system prompt is expanded to ~4.8k tokens (a richer few-shot set), so the cache **fires**: within a pipeline run, the 2nd+ `extract_claims` calls read the prefix from cache. In a representative 57-paper run, 54/57 calls hit the cache and per-call cost fell from ~$0.00205 (11a) to ~$0.0013 despite the larger prompt — see `docs/cost-baseline.md`.

The skeptic prompt (~840 tokens) is intentionally **not** padded to the cache threshold (only ~5 calls per sweep — caching wouldn't amortize); the skeptic gets its 50% savings from the Batch API instead (Phase 11d).

Verify cache activity via `usage.cache_read_input_tokens` in the AnthropicClient debug logs, the `llm_usage` ledger's `cache_read_tokens` column, or `mesh.cli cost report` (Cache R/W column).

### Troubleshooting

| Error | Meaning | Fix |
|---|---|---|
| `AnthropicNotReadyError: ANTHROPIC_API_KEY is not set` | Key missing | Add it to `.env`, restart the stack |
| `AnthropicNotReadyError: ... key was rejected` | Bad key | Regenerate in console |
| `AnthropicNotReadyError: model ... not found` | Wrong model ID | Use a valid ID from <https://docs.claude.com> |
| `AnthropicNotReadyError: rate limit exceeded` | Hit per-minute or daily limit | Wait, or upgrade tier |
| `LLMResponseError: no parsed output ... refusal=...` | Safety refusal on a paper | Pipeline skips that paper and continues; check the abstract |

---

## Ollama (local, offline)

To switch back to the local path:

```
MESH_LLM_PROVIDER=ollama
MESH_LLM_MODEL=qwen3:8b
```

Inside docker, the claim-extractor service reaches Ollama on the host via `OLLAMA_HOST=http://host.docker.internal:11434` (default).

### Install

```bash
# macOS / Linux
curl -fsSL https://ollama.com/install.sh | sh

# macOS via Homebrew
brew install ollama

ollama serve
```

Ollama listens on `http://localhost:11434` by default.

### Pull a model

The default is `qwen3:8b`. Pull it before running the pipeline:

```bash
ollama pull qwen3:8b
```

### Model recommendations

| Model | VRAM | Speed | Quality | When to use |
|-------|------|-------|---------|-------------|
| `qwen3:8b` | ~6 GB | fast | ★★★☆☆ | **Default** — fast and memory-efficient |
| `qwen3:14b` | ~10 GB | medium | ★★★★☆ | Better extraction quality; needs ~10 GB VRAM |
| `qwen3.6:27b` | ~20 GB | slow | ★★★★★ | High-end workstations / A100 class GPUs |
| `gemma3:27b` | ~20 GB | slow | ★★★★★ | Alternative; strong on structured output |

### Troubleshooting

**"model not found"** — pull it explicitly:

```bash
ollama pull qwen3:8b
ollama list   # see what's available locally
```

**Port conflict (address already in use)** — point Ollama at a different port:

```bash
OLLAMA_HOST=0.0.0.0:11435 ollama serve
```

Then in `.env`:

```
OLLAMA_HOST=http://localhost:11435
```

**Slow inference**

- Use a smaller model (`qwen3:8b` vs `qwen3:14b`).
- On macOS, Ollama uses Metal automatically — ensure nothing else is using the GPU heavily.
- Reduce `MESH_PIPELINE_CONCURRENCY` to `1`.

**Malformed JSON output** — qwen3 occasionally returns truncated or wrapped JSON. The pipeline handles this gracefully: `ClaimExtractorAgent` catches `LLMResponseError`, logs the failure, and returns an empty claims list for that paper. If it happens frequently, switch to a larger model or trim the system prompt in `packages/mesh-llm/src/mesh_llm/prompts.py`.

**Health check fails on pipeline start** — the pipeline calls `health_check()` before processing papers. Verify Ollama is reachable:

```bash
curl http://localhost:11434/api/tags
```

If on a different host/port, set `OLLAMA_HOST` in `.env`.

---

## Switching providers

The factory in `packages/mesh-llm/src/mesh_llm/factory.py` reads `MESH_LLM_PROVIDER` at construction time. To switch:

1. Change `MESH_LLM_PROVIDER` in `.env`.
2. `make down && make up` (the claim-extractor container picks up the new env via `env_file`).
3. `make pipeline` — the orchestrator's `make_llm_client()` will instantiate the new backend.

No code changes needed; both clients conform to the same `LLMClient` Protocol.

---

## Per-agent model routing

Different agents can use different models. `make_llm_client(agent_name=...)` resolves the model via this precedence chain (highest wins):

1. **`MESH_LLM_MODEL_<AGENT>`** — per-agent override. E.g. `MESH_LLM_MODEL_SKEPTIC=claude-sonnet-4-6`, `MESH_LLM_MODEL_EXTRACTION=claude-haiku-4-5`. Agent names today: `extraction`, `skeptic`, `personalizer`. (More as new LLM-backed agents land.)
2. **`MESH_LLM_MODEL_DEFAULT`** — workspace-wide override applied to any agent without a per-agent var.
3. **`MESH_LLM_MODEL`** — legacy single-model env from Phase 3. Still honored for back-compat.
4. **Provider hard-coded fallback** — `claude-haiku-4-5` for Anthropic, `qwen3:8b` for Ollama.

Example: run the skeptic on Opus while keeping extraction cheap on Haiku:

```bash
export MESH_LLM_MODEL_DEFAULT=claude-haiku-4-5
export MESH_LLM_MODEL_SKEPTIC=claude-opus-4-7
make pipeline   # extraction uses Haiku
make skeptic    # skeptic uses Opus, all other agents use Haiku
```

Verify by grepping the model string in container logs after a run.

### Finalized routing (Phase 11e)

Only **three** skills call an LLM; everything else in the fleet (the 7 scouts,
entity-tracker, sota-tracker, curator) is rule-based and uses **no model**, so
there is nothing to route for them. The audit found nothing over-provisioned —
the cost-optimal tier for every LLM agent is already the cheapest current-gen
model, `claude-haiku-4-5`.

| Agent (`agent_name`) | Task | Model | Rationale |
|---|---|---|---|
| Claim extractor (`extraction`) | Structured extraction from one abstract | **`claude-haiku-4-5`** | Cheapest current tier; the 4.8k few-shot prompt is cached (11c) so it's cheap and high-recall. A larger model isn't justified for span-level extraction. |
| Skeptic (`skeptic`) | Falsification reasoning over a belief + its claims | **`claude-haiku-4-5`** (default) | Reasoning task — the principle allows a larger tier. Haiku produced sound verdicts in testing, and the sweep is now batched (11d, −50%). If falsification quality proves thin, `MESH_LLM_MODEL_SKEPTIC=claude-sonnet-4-6` is the recommended upgrade; batching keeps it affordable. |
| Personalizer (`personalizer`) | Rank/curate the user-facing daily brief | **`claude-sonnet-4-6`** (recommended) | User-facing synthesis where quality is visible, and it runs **once per day** over a small candidate set — negligible cost impact. `.env.example` ships this default. Drop to Haiku if cost-sensitive. |
| Scouts, entity/sota tracker, curator | Feed parsing, find-or-create, rule-based selection | **none** | No LLM call — pure Python. |

**Why no downgrades:** the phase targets cost reduction by removing
over-provisioning, but every LLM agent already sits on the cheapest 4.x tier
(Haiku 4.5). Haiku 3.5 is nominally cheaper (~$0.80/Mtok in) but lower quality;
extraction recall matters (11c), so we keep Haiku 4.5. The remaining levers were
dedup (11b), caching (11c), and batching (11d), not downgrades.

Verify the model in use by grepping the model string in container logs, or via
`mesh.cli cost report` (Model column).
