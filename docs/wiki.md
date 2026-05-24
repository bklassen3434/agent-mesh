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
│   └── deps.get_conn() → READ_ONLY DuckDB connection per request │
└────────────────────────────┬────────────────────────────────────┘
                             │ duckdb file (volume: mesh-data)
                             ▼
                      mesh.db (DuckDB)
                             ▲
                             │ writes (only when running)
                             │
            apps/pipeline/coordinator (batch, on demand)
```

## Why API-in-front-of-DuckDB

We could have had Next.js open the DuckDB file directly. We chose not to:

1. **DuckDB is single-writer.** Sharing a file with the coordinator from a
   long-lived Node process is messy; one bad open and the coordinator can't
   write. A Python process in front of the file gives us a single, narrow
   place to enforce read-only.
2. **The API is reusable.** Today it powers the wiki. Tomorrow it powers an
   MCP server, a CLI subcommand, a notebook. None of those should reimplement
   the join logic for "belief with full provenance."
3. **Query logic stays where the models live.** The Pydantic models in
   `packages/mesh-models` are the source of truth. Putting query code next
   to them keeps the typed-DB layer and the typed-API layer aligned.

## Read-only coexistence with the coordinator

The locked decision: the API opens DuckDB in `READ_ONLY` mode. This is what
allows it to coexist with the coordinator's writes without lock contention.

A subtlety that matters in practice: DuckDB's process-level locking means a
*long-lived* read-only connection might not reflect writes the coordinator
commits while the API is running. To sidestep that entirely, the API opens
a **fresh** connection per request:

```python
def get_conn() -> Iterator[duckdb.DuckDBPyConnection]:
    conn = get_connection(read_only=True)
    try:
        yield conn
    finally:
        conn.close()
```

On every request: open, query, close. The cost is negligible (DuckDB cold
opens are millisecond-fast), and the invariant is bulletproof — whatever is
committed on disk is what the next request sees.

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
