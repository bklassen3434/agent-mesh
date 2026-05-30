# Wiki (Phase 3)

The read-only wiki layer that makes the mesh's accumulated knowledge legible
to humans. Two new services, both long-lived behind `make up`:

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

## What this phase explicitly does NOT do

Auth, write paths, full-text/vector search, SSE/live updates, charts beyond
the revision timeline, markdown rendering of excerpts, dark-mode toggle,
animations, notifications, deployment outside `localhost`. Those are Phase 4+
and the locked decisions for Phase 3 say so. Resist scope creep.
