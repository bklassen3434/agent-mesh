-- Phase 11a: per-call LLM token + cost ledger. One row per LLM skill call,
-- written by the coordinator / skeptic-sweep (the single DuckDB writer) from
-- usage threaded back through the A2A skill response. `run_id` joins to
-- pipeline_runs so `mesh.cli cost report` can aggregate per-skill cost for a run.
CREATE TABLE IF NOT EXISTS llm_usage (
    id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    agent_name VARCHAR,
    skill_id VARCHAR NOT NULL,
    model VARCHAR,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd DOUBLE NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_run_id ON llm_usage (run_id);
