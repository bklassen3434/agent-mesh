# Postgres Migration (Phase 12)

Consolidating the knowledge store from embedded DuckDB (`duckdb-vss`) onto the
Postgres instance already running for LangGraph checkpoints + the `schedules`
table. One store; `pgvector` replaces `duckdb-vss`; one schema/migration/backup
story; cross-store queries (e.g. `pipeline_runs` ⋈ `claims`) become possible.

**This is a storage swap, not a redesign.** Tables, columns, constraints, and the
coordinator-owned-write model are ported faithfully. No new features.

This document is the design locked by **Sub-Phase 12a** (validation spike). It is
updated as the later sub-phases execute so it ends as the as-built record.

---

## 1. Validation spike — does Postgres handle the analytical workload?

The one genuine risk in this migration is performance: the derived-signal queries
(Phase 7b) are a columnar-shaped aggregate workload, and Postgres is row-oriented.
12a de-risks this before any production change.

### Method

- **Real base data.** A real pipeline run populated the dev DuckDB (25 entities,
  40 sources, 45 claims). The local in-process orchestrator only synthesizes
  beliefs from SOTA-shaped claims, and this sample produced none, so beliefs /
  relationships were synthesized for the spike — anchored on the real claim
  shapes (predicate mix, `object` JSON shapes, source-type spread, skeptic
  failure modes) and amplified to a generous **multi-year** projected volume:

  | table | rows |
  |---|---|
  | entities | 5,025 |
  | sources | 15,040 |
  | claims | 50,045 |
  | beliefs | 8,000 (95% currently-held) |
  | relationships | 5,000 |

  (At ~20–40 papers/run, a few runs/day, real volume reaches this only after
  ~1–2 years, so these timings are conservative.)

- **Throwaway Postgres:** `pgvector/pgvector:pg16` (PostgreSQL 16.14, pgvector
  **0.8.2**), default config (`work_mem=4MB`). Spike harness lives at
  `/tmp/pg_spike.py` (not committed — 12a makes zero production changes).
- The three derived-signal views (`belief_reproduction`, `belief_signals`,
  `belief_hype_substance`) and the `/graph/data` top-200 aggregate were
  hand-ported to Postgres dialect (see §6) and timed as plain views, then again
  as a materialized view. Median of 5 warm runs (cold run dropped).

### Results (median warm)

| Query | Plain view | Materialized |
|---|---:|---:|
| `belief_hype_substance` — **full scan** (all 7,600 held beliefs) | **206 ms** | 8 ms |
| `belief_reproduction` — full scan | 119 ms | — |
| `graph_nodes` — top-200 by belief count | 70 ms | — |
| `belief_hype_substance` — **single belief** (`WHERE belief_id=…`) | **0.9 ms** | 0.2 ms |
| `mv_hype` REFRESH (full rebuild) | — | 204 ms |
| pgvector HNSW build (3,000 × 384-dim) | 107 ms | — |
| pgvector 5-NN cosine similarity | <1 ms, sensible neighbors | — |

`EXPLAIN ANALYZE` on the full-scan worst case: hash joins over the unnested
claim-id arrays, ~236 ms total, with a small `work_mem` spill (`external merge
Disk: 4080kB`) that a modest `work_mem` bump would remove.

### Interpretation

- **Plain views are acceptable.** The columnar-vs-row worry does not materialize at
  representative scale. The single heavy number is the *whole-set* scan (206 ms),
  which corresponds to exactly one endpoint: `GET /beliefs/signals` with no `ids`
  (the un-paginated "all held beliefs" badge fetch). Every other access pattern —
  per-belief detail, the `ids IN (...)` batch for a paginated list — is
  sub-millisecond to low-ms.
- **Materialized views are an easy 25× win** if that full-scan endpoint ever
  becomes hot, but they introduce staleness + a refresh trigger and are **not
  required** to ship. DuckDB's current views are also recomputed-on-read, so plain
  Postgres views are the faithful port.

### Decision: plain views, matviews held in reserve. **GO.**

---

## 2. Schema layout

**Decision: a dedicated `knowledge` Postgres schema** for the migrated tables,
inside the existing Postgres database, alongside the operational tables
(`schedules`, and the LangGraph checkpoint tables in `public`).

Rationale: keeps the conceptual knowledge/operational split that DuckDB-vs-Postgres
gave us, while consolidating infrastructure to one server. It also makes the
role/grant story clean (grants scoped per-schema, §3) and keeps the checkpoint
tables — which we must not touch — isolated in `public`.

- Knowledge tables → `knowledge.*` (entities, sources, claims, beliefs,
  belief_revisions, relationships, investigations).
- Operational tables → wherever they already live: `schedules` and the LangGraph
  checkpoint tables stay in `public`, **unchanged**.
- `pipeline_runs`, `llm_usage`, `processed_items` are operational ledgers but are
  written by the same coordinator and are useful joined to knowledge (e.g.
  `pipeline_runs ⋈ claims`). **Decision: place them in `knowledge` too** so the
  coordinator's writes are all one grant domain and cross-joins need no
  cross-schema search-path juggling. (They are not LangGraph's; nothing in
  `public` depends on them.)

---

## 3. Write-ownership enforcement (preserved, now via roles)

Today the coordinator-owned-write rule is enforced implicitly by DuckDB's
single-writer file lock. **This phase preserves the behavior exactly** (agents do
NOT write directly; the coordinator owns all knowledge writes — see Out of Scope in
the phase plan) and additionally enforces it with Postgres roles:

| Role | Grants | Used by |
|---|---|---|
| `mesh_writer` | `USAGE` on `knowledge`; `SELECT, INSERT, UPDATE` on `knowledge.*` (no `DELETE` — claims/revisions are immutable/append-only) | coordinator, skeptic-sweep, CLI `init-db`/migrations |
| `mesh_reader` | `USAGE` on `knowledge`; `SELECT` only on `knowledge.*` (tables + views) | `apps/api` |

- The API connects as `mesh_reader`, so a read-only posture is enforced by the DB,
  not just by convention (it currently relies on `read_only=True` on the DuckDB
  connection). The Phase 9 schedule writes (`schedules` in `public`) stay on their
  existing connection/role — out of the knowledge grant domain.
- `DELETE` is withheld from `mesh_writer` to back the **immutability invariants**
  (claims never deleted, revisions append-only). `superseded` status is an
  `UPDATE`, which is allowed.
- Default privileges (`ALTER DEFAULT PRIVILEGES IN SCHEMA knowledge …`) are set so
  tables/views created by future migrations inherit the same grants.
- Migrations run as an owner/superuser role (or the DB owner); the runtime roles
  above are least-privilege.

---

## 4. pgvector strategy

Current `duckdb-vss` usage is **latent**: `entities.name_embedding FLOAT[384]`
exists but is unpopulated and **no query reads it** (Phase 1 reserved it for entity
resolution; that is the *next* phase). So 12 only needs to port the column and
stand up an index strategy that the entity-resolution phase can switch on.

- **Extension:** `CREATE EXTENSION IF NOT EXISTS vector;` (pgvector 0.8.2 confirmed
  in the image).
- **Column:** `knowledge.entities.name_embedding vector(384)` (nullable, as today).
- **Distance:** cosine — `<=>` operator with `vector_cosine_ops` (matches the
  semantic-similarity intent of entity-name matching; normalize or use cosine to
  stay scale-invariant).
- **Index type:** **HNSW** (`USING hnsw (name_embedding vector_cosine_ops)`).
  Verified against pgvector 0.8 docs and the spike: HNSW gives better
  query-speed/recall than IVFFlat and needs no training step / `lists` tuning, at
  the cost of slower build + more memory. At our entity scale (thousands, not
  millions) build is trivial (107 ms for 3,000 vectors). IVFFlat remains the
  fallback only if entity count later reaches the millions and build/memory
  pressure shows up.
- **When to build:** the index is only worth creating once embeddings are actually
  populated (entity-resolution phase). For 12b we create the column now and may
  create the empty-friendly index; the spike confirms the build + a 5-NN query
  both work.

---

## 5. Migration tooling

**Decision: extend the repo's existing lightweight pattern — numbered SQL
migrations applied idempotently via `psycopg` — rather than adopt Alembic.**

Today there are two precedents:
- `packages/mesh-db/migrations/NNN_*.sql` + an idempotent runner
  (`apply_migrations()`) that tracks applied files in a `migrations` table. This is
  the knowledge schema's existing migration story.
- `mesh_a2a.schedules` / the LangGraph checkpointer create their Postgres schema
  with inline idempotent DDL (`CREATE TABLE IF NOT EXISTS`, `saver.setup()`); there
  is *no* general Postgres migration framework in the repo.

Plan: keep the **numbered `NNN_*.sql`** convention (it is already the team's mental
model and the files are easy to review) but point the runner at Postgres via
`psycopg` instead of DuckDB. The runner keeps the same `migrations` bookkeeping
table (now in `knowledge`) and the same "apply only unapplied, in filename order,
one transaction each" semantics. Statements that DuckDB needed split (`INSTALL`/
`LOAD`) disappear; Postgres handles multi-statement scripts per transaction
cleanly.

Adopting Alembic was considered and rejected for this phase: it adds a dependency
and a second migration mental model for marginal benefit on a schema this size, and
the phase mandate is "match whatever already manages the Postgres schema." Revisit
if/when the schema starts needing programmatic data migrations or branching.

---

## 6. Views vs materialized views (per derived signal)

| Object | Decision | Refresh strategy |
|---|---|---|
| `belief_reproduction` | **Plain view** | n/a (recomputed on read, as in DuckDB) |
| `belief_signals` | **Plain view** | n/a |
| `belief_hype_substance` | **Plain view** | n/a |
| `/graph/data` aggregate | **Plain view / inline query** | n/a |

All four stay plain views — the faithful port, no staleness, no refresh machinery,
and the spike shows they are fast enough for every real access pattern.

**Reserved optimization (documented, not built):** if `GET /beliefs/signals` (the
un-paginated all-held-beliefs badge fetch) becomes a latency hotspot, promote
`belief_hype_substance` to a materialized view (`mv_belief_hype_substance`) with a
unique index on `belief_id`. Refresh trigger options, in order of preference:
1. `REFRESH MATERIALIZED VIEW CONCURRENTLY` at the end of each coordinator pipeline
   run + skeptic sweep (the only writers; ~200 ms, off the request path), or
2. periodic refresh from the scheduler.
Not adopted now — flagged so the decision is on record.

---

## 7. DuckDB → Postgres type & idiom mapping (12b/12d reference)

Derived from reading every migration in `packages/mesh-db/migrations/` and every
query in the `mesh-db` access layer. Authoritative table inventory: `entities`,
`sources`, `claims`, `beliefs`, `belief_revisions`, `relationships`,
`investigations`, `pipeline_runs`, `llm_usage`, `processed_items`, plus the
`migrations` bookkeeping table and the three derived views.

### Types

| DuckDB | Postgres | Notes |
|---|---|---|
| `VARCHAR` | `TEXT` | |
| `VARCHAR[]` (e.g. `aliases`, `supporting_claim_ids`) | `TEXT[]` | DuckDB list default `DEFAULT []` → Postgres `DEFAULT '{}'` |
| `JSON` (`object`, `attributes`, `errors`) | `JSONB` | enables `->>`/`->`/GIN indexing |
| `DOUBLE` | `DOUBLE PRECISION` | |
| `INTEGER`, `BOOLEAN`, `TIMESTAMPTZ` | same | |
| `FLOAT[384]` (`name_embedding`) | `vector(384)` | pgvector (§4) |

### Query idioms (must be hand-ported — do not pattern-match)

| DuckDB | Postgres |
|---|---|
| `UNNEST(arr)` (in CTE) | `unnest(arr)` — works; mind array column type |
| `json_extract_string(obj, '$.k')` | `obj->>'k'` |
| `json_extract(obj, '$.k')` | `obj->'k'` |
| `cast(obj AS VARCHAR)` | `obj::text` |
| `printf('%.1f', x)` | `to_char(x::double precision, 'FM999999990.0')` |
| `INTERVAL 30 DAY` | `INTERVAL '30 days'` |
| `len(arr)` (list length) | `cardinality(arr)` / `array_length(arr,1)` |
| `now()`, `TIMESTAMPTZ '1970-01-01'` | identical |
| `?` positional params (DuckDB/`duckdb` driver) | `%s` (psycopg) — every call site |
| array literal default `DEFAULT []` | `DEFAULT '{}'` |

The ported derived-signal view SQL validated in the spike is the reference
implementation for 12b; it is reproduced in `/tmp/pg_spike.py` (12a) and will land
in a migration in 12b.

> ⚠️ **psycopg `%` escaping:** in parametrized `execute()` calls, literal `%`
> (modulo, `LIKE '%x%'`, `to_char` patterns) must be doubled to `%%`. Caught twice
> in the spike — call it out in 12d code review.

---

## 8. Go / No-Go

**GO.**

- Postgres handles the derived-signal workload comfortably at conservative
  multi-year volume with **plain views** (worst case 206 ms for a single
  whole-set endpoint; everything else low-ms). No redesign or matview required to
  ship; matview promotion is documented and held in reserve.
- `pgvector` 0.8.2 cleanly replaces `duckdb-vss`: HNSW index builds, cosine
  similarity returns sensible neighbors. The embedding column is latent today, so
  this carries zero behavioral risk for 12.
- Schema layout (`knowledge` schema), role/grant model (writer/reader, no
  `DELETE`), and migration tooling (numbered SQL via `psycopg`) all fit the
  existing repo patterns without new dependencies.
- Write-ownership behavior is preserved and additionally hardened by roles.

Proceed to **12b — Postgres schema + migrations.**

---

## 9. As-built log

### 12b — schema + migrations (done)
- Migrations live in `packages/mesh-db/migrations_pg/` as a **consolidated
  fresh-state baseline** (not a replay of the 18 incremental DuckDB migrations):
  `001_extensions` (pgvector), `002_core_tables` (the 7 knowledge entities +
  indexes), `003_operational_tables` (pipeline_runs / llm_usage /
  processed_items), `004_derived_signal_views`, `005_grants`.
- Runner: `mesh_db.pg_migrations` (psycopg3) — `ensure_roles` (env-driven
  `mesh_writer`/`mesh_reader` login roles) then `apply_pg_migrations` (idempotent,
  numbered, per-file transaction, tracked in `knowledge.migrations`). Exposed as
  `mesh.cli init-pg-db` and `python -m mesh_db.pg_migrations`. `psycopg[binary]`
  added to `mesh-db` deps. The DuckDB path is untouched (removed in 12d/12e).
- Gotcha fixed: the statement splitter strips `--` comments *before* splitting on
  `;` (comments legitimately contain semicolons). Regression test in
  `tests/test_pg_migrations.py`.
- New env vars: `MESH_PG_URL` (knowledge DSN; falls back to
  `LANGGRAPH_POSTGRES_URL`), `MESH_WRITER_PASSWORD` / `MESH_READER_PASSWORD`
  (defaults `mesh_writer` / `mesh_reader`). To be documented in `.env.example` /
  compose in 12e.
- **Gates verified** on a fresh DB: 11 tables + 3 views; 7 scalar provenance FKs
  enforced; `processed_items` composite PK; `name_embedding` is `vector(384)`;
  HNSW index builds + cosine k-NN returns self first; `belief_hype_substance`
  computes correctly on seed data (diversity=3, repro=3 → 0.9375, matching the
  DuckDB formula); `mesh_writer` can INSERT but **not** DELETE, `mesh_reader` is
  SELECT-only. Full offline suite (383 tests), mypy strict, ruff all pass.

### 12c — data migration (done)
- `mesh_db.duckdb_to_pg`: one-time, idempotent (truncate-and-reload) DuckDB →
  Postgres copy. Reads every row, preserves PKs/FKs. Insert order is FK-safe;
  `claims.superseded_by_claim_id` (self-ref) is filled in a second pass so claim
  order is irrelevant. JSON columns are read `::VARCHAR` from DuckDB and written
  `::jsonb`; `VARCHAR[]` → `text[]` via psycopg list adaptation; `FLOAT[384]` →
  `'[..]'::vector`. Exposed as `mesh.cli migrate-duckdb-to-pg` /
  `python -m mesh_db.duckdb_to_pg`.
- Built-in `verify()`: per-table row-count parity, no orphaned claims, belief +
  relationship claim-id arrays all resolve to real claims, skeptic source rows
  preserved. The runner exits non-zero if any check fails.
- **Verified** on the real dev DB (25 entities / 40 sources / 45 claims / 1
  investigation / 3 runs — counts match, idempotent across two runs) **and** on a
  rich synthetic fixture exercising the paths the thin dev data can't: a
  superseded self-ref (`c_old→c1` survived), belief claim-id arrays
  (`['c1','c2']` / `['c_sk']` survived), a skeptic counter-claim with failure
  mode (`belief_signals` → diversity=2, skeptic=1, severe=1), and a populated
  384-dim embedding migrated into pgvector with a working cosine k-NN.
- Offline regression tests for the pure helpers in `tests/test_duckdb_to_pg.py`
  (live migration needs both stores, so it's covered by the verification
  harness, mirroring 12b).

### 12d — access-layer rewrite (done)
- `connection.py` now returns a `MeshConnection` proxy over a `psycopg_pool`
  ConnectionPool: same call-site contract (`execute`/`fetchone`/`fetchall`/
  `close`), but `close()` returns the connection to the pool. Writer/reader
  pools by DSN (`MESH_PG_WRITER_URL`/`MESH_PG_READER_URL`, falling back to the
  base owner DSN). Autocommit (matches DuckDB's implicit commit). The pool's
  `configure` sets `search_path TO knowledge, public`, so all unqualified table/
  view references resolve without rewriting every query.
- Every `mesh-db` query ported to Postgres dialect: `?`→`%s`, JSON writes wrapped
  in `Jsonb()` (reads already tolerate dict-or-str), `len()`→`cardinality()`.
  `UNNEST`, `any_value` (PG16), `ON CONFLICT … excluded`, `ILIKE`, `TIMESTAMPTZ`
  literals and `NULLS FIRST` are valid Postgres as-is. Agent SQL ported too —
  the DuckDB `list_filter(aliases, x -> …)` lambda became
  `EXISTS (SELECT 1 FROM unnest(aliases) …)`. API router raw SQL ported.
- Schema provisioning decoupled from the runtime connection: the five
  `apply_migrations(conn)` startup sites (coordinator, skeptic-sweep,
  orchestrator, API, CLI) now call `init_pg()` (owner connection) — the
  writer/reader roles can't run DDL.
- Public method signatures unchanged; call sites only swapped the connection
  type annotation. Read each query — no blanket transforms (caught a regex `?`
  in sota_tracker and a Python `"?"` default in the status router).
- **Tests** moved to testcontainers: a session pgvector/pg16 container, schema +
  roles via `init_pg`, an autouse truncate for per-test isolation, and a
  `tmp_db` that yields a pooled connection. The DuckDB read-only-file test was
  rewritten to assert the real `mesh_reader` role rejects writes. Full suite
  (393), mypy strict, ruff all green against Postgres.

### Open items carried into later sub-phases
- Confirm `pipeline_runs`/`llm_usage`/`processed_items` schema placement against the
  `/status` reader and cost CLI when wiring 12e.
- Decide `work_mem` for the API connection if the full-scan signals endpoint stays
  un-paginated (or paginate it).
- Build the HNSW index only once entity-resolution populates embeddings.
