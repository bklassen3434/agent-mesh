# Personalization

The Personalizer agent filters the mesh's daily output against a profile you
write in plain markdown. The wiki's `/briefing` route renders the result.

## How it works

1. The wiki calls `GET /api/v1/briefing?date=YYYY-MM-DD` (default = today).
2. The API loads your profile from `$MESH_PROFILE_PATH`
   (default `~/.config/agent_mesh/profile.md`). Missing profile → HTTP 404.
3. The API queries DuckDB for the 24h window of the target date: new beliefs,
   belief revisions, and high-confidence claims.
4. If there are no candidates, the API returns a "Quiet day" briefing
   immediately — no LLM call.
5. Otherwise the API dispatches `personalize_digest` to the Personalizer agent
   over A2A, polls until done, and returns the ranked `Briefing`.
6. The result is cached in process memory keyed by `(date, profile_hash)`.
   Editing the profile invalidates that day's cache automatically.

The Personalizer is pure: it never reads the DB. The API orchestrates all
candidate gathering, the agent ranks. This keeps the agent boundary clean and
matches the rest of the mesh.

## Authoring a profile

Create `~/.config/agent_mesh/profile.md` (or set `MESH_PROFILE_PATH` to point
elsewhere). Plain markdown — no schema, no front-matter. The LLM reads it
verbatim.

The profile is most useful when it answers three questions:

1. **Who are you and what do you build?** Role, stack, the kind of system you
   care about.
2. **What topics matter right now?** Be specific — "production LLM
   observability and eval harnesses" beats "AI".
3. **What's noise?** Telling the model what to deprioritize is as valuable as
   telling it what to surface.

### Example

```markdown
# Profile

I'm an AIOps engineer at a large financial-services company. My day is mostly
spent on production observability and incident response for LLM-backed
agentic workflows.

## High interest

- Production LLM observability — tracing, eval harnesses, regression detection
- Agent evaluation benchmarks (especially anything resembling real tool use)
- Inference-time scaling techniques (speculative decoding, batching strategies)
- Open-source models in the 7-70B range that are practical on a single H100

## Low interest

- Image and video generation
- Robotics / embodied AI
- AGI / safety philosophy posts without concrete results
- Anything that's just a marketing announcement with no benchmarks
```

The profile is read on every request, so iteration is free — edit, refresh
the briefing, see different rankings.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MESH_PROFILE_PATH` | `~/.config/agent_mesh/profile.md` | Markdown profile file. |
| `MESH_LLM_MODEL_PERSONALIZER` | `claude-sonnet-4-6` | Per-agent model override. Sonnet is preferred because ranking with rationale benefits from the larger model; falls through to `MESH_LLM_MODEL` if unset. |
| `MESH_BRIEFING_AGENT_URLS` | `http://personalizer:8013` | Comma-separated A2A agent URLs the briefing endpoint discovers. Set to `http://localhost:8013` if running the personalizer outside docker. |
| `MESH_TASK_TIMEOUT_PERSONALIZE_DIGEST` | (uses `MESH_TASK_TIMEOUT_DEFAULT`) | Per-skill timeout for the personalize task. The default of 120s is comfortable for Sonnet over ~50 candidates. |

## Endpoints

- `GET /api/v1/briefing` — today's briefing.
- `GET /api/v1/briefing?date=2026-05-25` — a previous day's briefing,
  recomputed from the historical window.

## Wiki

`/briefing` renders the digest. Sections are populated by the Personalizer
prompt (typically "New Beliefs", "Belief Revisions", "Hot from Skeptic",
"Worth Reading"). Each item shows a relevance score, a per-item rationale,
and a link into the underlying belief or claim.

- The route is server-rendered. The Personalizer call can take 10-30s; while
  it runs you see a skeleton via `loading.tsx`.
- If no profile is configured, the route shows a friendly empty state with
  setup instructions instead of erroring.

## What is NOT stored

- No `Briefing` DB table. Briefings are recomputed on demand.
- No persistent cache — restarting the API clears the in-memory cache.
- The profile lives on disk as a file, not in the DB.

This is by design: briefings are a *view* over the mesh, not a write path.
The API stays read-only.
