# Wiki (Phase 3, expanded through Phase 23)

The wiki layer that makes the mesh's accumulated knowledge legible to humans.
Originally a strictly read-only window onto the knowledge store (Phase 3), it
has since grown into the system's primary control surface — see
[What the wiki is now (Phase 9–23)](#what-the-wiki-is-now-phase-923). The core
rationale below is unchanged: a thin Python read API in front of Postgres, with
Next.js as one consumer of a typed JSON contract.

Two long-lived services, both behind `make up`:

```
┌─────────────────────────────────────────────────────────────────┐
│  apps/wiki (Next.js 15, App Router)         :3000               │
│   ├── server components fetch via INTERNAL_API_URL              │
│   └── client (browser) hits NEXT_PUBLIC_API_URL                 │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTP/JSON
┌────────────────────────────▼────────────────────────────────────┐
│  apps/api (FastAPI)                          :8000              │
│   ├── /healthz  /openapi.json  /docs                            │
│   ├── /api/v1/{stats, pipeline-runs}                            │
│   ├── /api/v1/{entities, claims, beliefs, sources}              │
│   └── deps.get_conn() → read-only Postgres (mesh_reader) per request │
└────────────────────────────┬────────────────────────────────────┘
                             │ Postgres (mesh-postgres)         
                             ▼
                      mesh-postgres (PG + pgvector)
                             ▲
                             │ writes (only when running)
                             │
            apps/pipeline/coordinator (batch, on demand)
```

## Why API-in-front-of-Postgres

We could have had Next.js query Postgres directly. We chose not to:

1. **Writes are coordinator-owned.** Only the coordinator writes the knowledge store (via the `mesh_writer` role); the API connects as the read-only `mesh_reader` role. A single Python service in front is the one narrow place to enforce that — Next.js never gets write-capable DB credentials.
2. **The API is reusable.** Today it powers the wiki. Tomorrow it powers an
   MCP server, a CLI subcommand, a notebook. None of those should reimplement
   the join logic for "belief with full provenance."
3. **Query logic stays where the models live.** The Pydantic models in
   `packages/mesh-models` are the source of truth. Putting query code next
   to them keeps the typed-DB layer and the typed-API layer aligned.

## Read-only coexistence with the coordinator

The locked decision: the API connects as the read-only `mesh_reader` role. Postgres MVCC lets it coexist with the coordinator's writes without contention, and the role's grants make read-only a hard guarantee, not a convention.

The API draws a **pooled** connection per request, so every request sees the latest committed state with no reconnect bookkeeping:

```python
def get_conn() -> Iterator[MeshConnection]:
    conn = get_connection(read_only=True)
    try:
        yield conn
    finally:
        conn.close()
```

On every request: check out from the pool, query, return. The cost is negligible (pooled connections, no TCP/auth churn), and the invariant holds — whatever the coordinator has committed is what the next request sees.

Bootstrap is the only exception: at startup the API briefly opens read-write
to apply (idempotent) migrations, then it never opens read-write again.

## How types flow

```
Pydantic models  (packages/mesh-models/*.py)
        │
        ▼  FastAPI response_model
OpenAPI /openapi.json   (live, served by the API)
        │
        ▼  openapi-typescript@7  (npm run generate-types  /  make types)
apps/wiki/src/lib/api-types.ts    (committed)
        │
        ▼  apps/wiki/src/lib/api.ts wraps fetch with strict types
Server components render typed data; no manual type maintenance.
```

CI runs `npm run generate-types` against a freshly booted API and fails the
job if the checked-in `api-types.ts` is stale. The diff guard is the contract.

## Adding a new endpoint

1. Add the read function in `packages/mesh-db/` if needed.
2. Add a response model in `apps/api/src/mesh_api/schemas.py` (or reuse a
   `mesh-models` Pydantic model).
3. Add the route in `apps/api/src/mesh_api/routers/`. Register it from
   `main.py`.
4. Add a TestClient case under `tests/api/`.
5. Regenerate types: `make up && make types` (or boot the API locally and
   run `npm run generate-types`).
6. Add the wiki page(s) that consume it.

## What the wiki is now (Phase 9–23)

The Phase 3 wiki was a handful of read-only knowledge tables. It has grown into
the system's primary UI without abandoning the API-in-front-of-Postgres model.

### Stack

Next.js 15 App Router, mostly **server components** (they fetch via
`INTERNAL_API_URL` and render typed data). Interactive bits — the nav
dropdown/drawer, the Ask chat, the graph — are
**client components** built on Radix-based **shadcn primitives** in
`apps/wiki/src/components/ui/` (`button`, `card`, `table`, `badge`,
`dropdown-menu`, `sheet`, `select`, `slider`, `switch`). Types still live in
`apps/wiki/src/lib/api-types.ts`, generated from the API's OpenAPI by
`make types` (see [How types flow](#how-types-flow)).

### Navigation

The nav is now:

```
Agent Mesh   Daily Brief · Ask · Knowledge ▾ · Graph · Agents · Fields · Connectors      mesh status →
```

`Knowledge ▾` is a dropdown over **Beliefs · Entities · Claims · Sources**,
which now live under `/knowledge/*`. The old top-level paths (`/beliefs`,
`/entities`, `/claims`, `/sources`, and their nested detail/timeline routes)
redirect to `/knowledge/*` via `next.config.js`, so old links keep working.
`mesh status →` is an out-of-band link to the operational status page.
(`nav-bar.tsx` is the source of truth.)

### Pages

| Route | Phase | What it is |
|---|---|---|
| `/briefing` (Daily Brief) | — | Personalized daily digest of new/changed knowledge |
| `/ask` | 21 | Knowledge chatbot — natural-language Q&A grounded in the store |
| `/knowledge/beliefs` `/entities` `/claims` `/sources` | 3 | The original knowledge tables + detail/timeline views |
| `/graph` | 9 | Force-directed Cytoscape view, fed by the pre-aggregated `/api/v1/graph/data` endpoint (top-200 nodes by belief count) |
| `/agents` | 23 | Agent observability — agent roster, the coordinator-star interaction graph, and per-agent drill-down (current memory + recent invocations → one invocation's inputs/outputs/context + Langfuse deep-link) |
| `/skeptic` | — | Skeptic sweep view |

### Reads are field-scoped

Every knowledge / cost / graph endpoint takes `?field=<slug>` (default
`ai-robotics`); the wiki passes the active field through (Phase 17). `field_id`
is a partition, never a content axis.

### Not strictly read-only at the edges anymore

The Phase 3 invariant — Next.js never holds write-capable DB credentials —
**still holds**. The knowledge store remains read-only from the wiki: the API
serves it as the `mesh_reader` role.

The one nuance: a small set of **non-knowledge** writes — the Ask `POST` and the
Fields/Connectors `PATCH`/`PUT` for per-field operational config. These do not
touch the knowledge store; knowledge still flows only through the controller's
`mesh_writer` role.

## Original Phase 3 boundary (historical)

> The following were the locked Phase-3-only exclusions. Much of this has since
> been built (vector search underpins entity/belief resolution and `/ask`;
> the graph view in Phase 9; agent observability in Phase 23). Kept here as a
> record of the original scope decision.

Auth, write paths, full-text/vector search, SSE/live updates, charts beyond
the revision timeline, markdown rendering of excerpts, dark-mode toggle,
animations, notifications, deployment outside `localhost`. Those were Phase 4+
and the locked decisions for Phase 3 said so. Resist scope creep.
