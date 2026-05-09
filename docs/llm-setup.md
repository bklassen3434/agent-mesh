# LLM Setup (Ollama)

Agent Mesh uses [Ollama](https://ollama.com) to run inference locally. No cloud API keys are needed.

## Install Ollama

```bash
# macOS / Linux
curl -fsSL https://ollama.com/install.sh | sh

# macOS via Homebrew
brew install ollama
```

Start the server:

```bash
ollama serve
```

Ollama listens on `http://localhost:11434` by default.

## Pull a model

The default model is `qwen3:14b`. Pull it before running the pipeline:

```bash
ollama pull qwen3:14b
```

## Model recommendations

| Model | VRAM | Speed | Quality | When to use |
|-------|------|-------|---------|-------------|
| `qwen3:14b` | ~10 GB | medium | ★★★★☆ | **Default** — best balance for most setups |
| `qwen3:8b` | ~6 GB | fast | ★★★☆☆ | Lower-memory machines (16 GB RAM); slightly more extraction errors |
| `qwen3.6:27b` | ~20 GB | slow | ★★★★★ | High-end workstations / A100 class GPUs |
| `gemma3:27b` | ~20 GB | slow | ★★★★★ | Alternative to qwen3.6:27b; strong on structured output |

### Switching models

Set the `MESH_LLM_MODEL` environment variable in your `.env`:

```
MESH_LLM_MODEL=qwen3:8b
```

The pipeline picks it up automatically via `OllamaClient(model=os.getenv("MESH_LLM_MODEL", "qwen3:14b"))`.

## Troubleshooting

### "model not found"

The model must be pulled before use. Pull it explicitly:

```bash
ollama pull qwen3:14b
```

Check what's available locally:

```bash
ollama list
```

### Port conflict (address already in use)

Another process is using port 11434. Either stop it or point Ollama at a different port:

```bash
OLLAMA_HOST=0.0.0.0:11435 ollama serve
```

Then set in `.env`:

```
OLLAMA_HOST=http://localhost:11435
```

### Slow inference

- Use a quantized model variant (e.g. `qwen3:8b` instead of `qwen3:14b`).
- On macOS, Ollama uses Metal automatically — ensure no other GPU-heavy workloads are running.
- Reduce `MESH_PIPELINE_CONCURRENCY` to `1` to serialize LLM calls and reduce memory pressure.

### Malformed JSON output

Qwen3 occasionally produces text outside the JSON block or truncated JSON when the abstract is very long. The pipeline handles this gracefully: `ClaimExtractorAgent` catches `LLMResponseError`, logs the failure, and returns an empty claims list for that paper.

If you see frequent parse failures, try:

1. **Switch to a larger model** — `qwen3.6:27b` is more reliable on long inputs.
2. **Trim the system prompt** — edit `packages/mesh-llm/src/mesh_llm/prompts.py`. Shorter, more directive prompts reduce hallucination on smaller models.
3. **Reduce `max_results`** — fewer papers per run means Ollama has more memory headroom per call.
4. **Check Ollama version** — `ollama --version`; structured output reliability improved significantly in 0.3+.

### Health check fails on pipeline start

The pipeline calls `OllamaClient.health_check()` before processing papers. If it raises `OllamaNotReadyError`, the pipeline aborts immediately.

Verify Ollama is reachable:

```bash
curl http://localhost:11434/api/tags
```

If the server is on a different host/port, set `OLLAMA_HOST` in `.env`.
