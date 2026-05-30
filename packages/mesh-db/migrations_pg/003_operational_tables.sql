-- Phase 12b: operational ledgers (pipeline_runs, llm_usage, processed_items).
-- Ported from DuckDB migrations 009-011, 017, 018. Placed in the `knowledge`
-- schema (see docs/postgres-migration.md §2): they are coordinator-written and
-- usefully joined to claims (e.g. cost-per-run), and nothing in `public`
-- (LangGraph checkpoints) depends on them.

CREATE TABLE knowledge.pipeline_runs (
    id                        TEXT PRIMARY KEY,
    started_at                TIMESTAMPTZ NOT NULL,
    finished_at               TIMESTAMPTZ,
    papers_scouted            INTEGER DEFAULT 0,
    sources_inserted          INTEGER DEFAULT 0,
    claims_inserted           INTEGER DEFAULT 0,
    entities_created          INTEGER DEFAULT 0,
    beliefs_created           INTEGER DEFAULT 0,
    beliefs_revised           INTEGER DEFAULT 0,
    avg_extraction_latency_ms INTEGER DEFAULT 0,
    errors                    JSONB DEFAULT '[]'::jsonb,
    -- migration 010 / 011
    run_type                  TEXT DEFAULT 'pipeline',
    triggered_by              TEXT DEFAULT 'manual'
);

CREATE TABLE knowledge.llm_usage (
    id                    TEXT PRIMARY KEY,
    run_id                TEXT NOT NULL,
    agent_name            TEXT,
    skill_id              TEXT NOT NULL,
    model                 TEXT,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd    DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at            TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_llm_usage_run_id ON knowledge.llm_usage (run_id);

CREATE TABLE knowledge.processed_items (
    source_type    TEXT NOT NULL,
    external_id    TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    first_seen_at  TIMESTAMPTZ NOT NULL,
    last_seen_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (source_type, external_id)
);
