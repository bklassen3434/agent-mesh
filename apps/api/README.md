# mesh-api

Read-only HTTP service in front of the mesh DuckDB. Backs the Next.js wiki and
is reusable as a generic JSON contract over the knowledge base.

## Run locally

```bash
uv run mesh-api          # listens on :8000
open http://localhost:8000/docs   # Swagger UI
```

In docker compose:

```bash
make up                  # api comes up alongside the four agents
make api                 # opens /docs in the browser
```

## Endpoints

All `GET`, all read-only. Pagination follows the envelope
`{ items, total, limit, offset }`. Default `limit=50`, max `limit=200`.

| Path | Notes |
|---|---|
| `/healthz` | liveness + whether the DB file exists |
| `/api/v1/stats` | counts of every entity type plus last-pipeline-run id/time |
| `/api/v1/pipeline-runs` | most recent runs, newest first; `?limit=1..200` |
| `/api/v1/entities` | `?type=&q=&limit=&offset=` |
| `/api/v1/entities/{id}` | entity + claims-as-subject + relationships |
| `/api/v1/claims` | `?predicate=&source_id=&entity_id=&status=&limit=&offset=` |
| `/api/v1/claims/{id}` | claim + source + subject entity |
| `/api/v1/beliefs` | `?topic=&currently_held=&limit=&offset=` |
| `/api/v1/beliefs/{id}` | composed view: supporting/contradicting claims (each with source + entity) + full revision history (each revision lists trigger claims). |
| `/api/v1/sources` | each row carries `claim_count`; `?type=&limit=&offset=` |
| `/api/v1/sources/{id}` | source + claims extracted from it |

The full schema is at `/openapi.json`. The Next.js wiki generates
TypeScript types from that file via `npm run generate-types` (`make types`).

## READ_ONLY and the coordinator

The API opens DuckDB in read-only mode on every request via a FastAPI
dependency (`apps/api/src/mesh_api/deps.py`). One open, one close per request:

- DuckDB allows multiple readers when no process holds the write lock.
- The coordinator is a short batch writer (`make pipeline`); it acquires the
  write lock for the duration of one pipeline cycle and releases it on exit.
- Per-request connections mean we always see committed changes after the
  coordinator finishes, without any reconnect bookkeeping.

For first-boot bootstrapping, the API briefly opens a read-write connection
at startup to apply (idempotent) migrations against a freshly mounted volume.
After that one-shot, all request handling is read-only.

## How the wiki consumes the API

```
Pydantic models (packages/mesh-models)
        ↓ FastAPI response_model
OpenAPI /openapi.json
        ↓ openapi-typescript
apps/wiki/src/lib/api-types.ts
        ↓ apps/wiki/src/lib/api.ts wraps fetch with strict types
Server components render typed data — no client state library needed.
```

Server components reach the API at `INTERNAL_API_URL` (e.g. `http://api:8000`
inside docker). Client components reach the API at `NEXT_PUBLIC_API_URL`
(`http://localhost:8000`), which is baked in at wiki build time.

## Tests

```bash
uv run pytest tests/api -v
```

Fixtures live in `tests/api/conftest.py`:

- `empty_client` — TestClient backed by a migrated but empty DB.
- `client` — TestClient backed by a small interconnected fixture set
  (entities, sources, claims, one belief with one revision, one pipeline run)
  that exercises the composed belief-detail shape end-to-end.
