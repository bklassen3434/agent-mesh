# mesh-wiki

Next.js 15 (App Router) read-only wiki for the Agent Mesh. Server components
fetch from the mesh-api; no client state libraries.

## Local development

In two terminals:

```bash
# 1. boot the API
uv run mesh-api

# 2. dev the wiki
cd apps/wiki
npm install
npm run dev
open http://localhost:3000
```

Or in docker:

```bash
make up
make wiki        # opens http://localhost:3000
```

## Type generation

TypeScript types in `src/lib/api-types.ts` are generated from the API's
`/openapi.json` by `openapi-typescript`. The file is committed; CI regenerates
and diffs to catch drift.

```bash
# with the API running on :8000
npm run generate-types
# or
make types
```

If the API contract changes, regenerate the types and commit the result with
the same PR.

## Architecture choices

- **Server components by default.** Routes are async server components that
  call `api.*` directly. No `useEffect`, no client-side data fetching for
  initial render. Client components are limited to the few places where real
  interactivity is needed (e.g. `src/components/entity-filter.tsx`).
- **No state libraries.** No Redux/Zustand/TanStack Query. Page refresh is
  the update mechanism.
- **shadcn-style primitives, hand-written.** `src/components/ui/*` mirrors
  the shadcn API (cva variants, `cn` helper) without depending on the
  interactive shadcn CLI. Add new primitives by writing them here directly.
- **Tailwind only.** No CSS-in-JS, no scoped styles.
- **Empty-state on every route.** A freshly cloned repo with no pipeline run
  renders gracefully.

## Routes

| Path | Purpose |
|---|---|
| `/` | Home dashboard: stat tiles, recent pipeline runs, recent belief revisions. |
| `/entities` | Paginated entity table with type + name-substring filters. |
| `/entities/[id]` | Entity detail + claims-as-subject + relationships. |
| `/beliefs` | Paginated belief list with topic filter. |
| `/beliefs/[id]` | **Belief detail with revision timeline.** The headline view. |
| `/claims` | Paginated claim table with predicate filter. |
| `/claims/[id]` | Claim detail: excerpt, object payload, source, subject entity. |
| `/sources` | Source list with claim counts. |
| `/sources/[id]` | Source detail with extracted claims. |

Every route segment has a `loading.tsx` (Suspense fallback) and `error.tsx`
(client component with retry).

## Production image

```bash
docker compose build wiki
```

`Dockerfile.wiki` produces a Next standalone server. The runtime image runs
as a non-root user on port 3000.
