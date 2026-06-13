-- Phase 23a: the agent-invocation record — durable per-skill-call capture.
--
-- One row per coordinator skill dispatch (claim-extractor, skeptic, curator,
-- the scouts/connectors, and — as later phases land — the belief-consolidator
-- and discovery agents). It answers the question no other surface exposes:
-- at a given moment, what was this agent thinking? — the input it received, the
-- output it produced, the memory/heuristic block it injected, the model it used,
-- and how this one invocation connects to the others in the run (run_id +
-- trace_id).
--
-- Invariants (mirrors llm_usage + agent_heuristic_revision):
--
--   * Coordinator-owned writes. Only mesh_writer writes this table (granted
--     below). Agents stay write-free per the role model; the coordinator holds
--     the input payload, the returned output, the traceparent, and the timing,
--     so it is the natural single writer.
--   * Append-only audit log. The writer gets SELECT/INSERT and crucially NO
--     DELETE — rows are never removed, matching claims / belief_revisions /
--     llm_usage. (Migration 005's ALTER DEFAULT PRIVILEGES also grants the
--     writer UPDATE on new tables, as it does for those append-only tables; the
--     code never issues one — there is no update_agent_invocation.) api-readonly
--     gets SELECT only.
--   * Field-scoped. Every row carries field_id (FK to fields); every read
--     filters by it. field_id is a partition, never a content axis.
--   * Bounded capture. input_summary / output_summary / memory_block are capped
--     summaries (the coordinator truncates to MESH_OBS_CAPTURE_MAX_CHARS); the
--     raw prompt/output lives in Langfuse, referenced by trace_id — never
--     duplicated wholesale into Postgres.
--
-- run_id is a plain indexed column, NOT a hard FK: the pipeline_runs row is only
-- written at finalize, while invocations are recorded as the run unfolds, so an
-- invocation can (briefly) exist before its run row does.

CREATE TABLE knowledge.agent_invocations (
    id                   TEXT PRIMARY KEY,
    -- run_id: groups invocations into one pipeline/sweep run. Plain indexed
    -- column (no FK — pipeline_runs is written only at finalize).
    run_id               TEXT NOT NULL,
    field_id             TEXT NOT NULL REFERENCES knowledge.fields(id),
    -- who + what: the dispatched agent and the skill it served.
    agent                TEXT NOT NULL,
    skill                TEXT NOT NULL,
    -- W3C trace plumbing: the threaded traceparent and its extracted trace id
    -- (the deep-link key into Langfuse for the raw prompt/output).
    traceparent          TEXT,
    trace_id             TEXT,
    -- outcome: "ok" | "error"; error_* mirror the TaskError fields.
    status               TEXT NOT NULL,
    error_type           TEXT,
    error_message        TEXT,
    -- bounded captures (capped by the coordinator; raw content stays in Langfuse).
    input_summary        JSONB,
    output_summary       JSONB,
    -- memory the agent injected, when it supplies the optional debug envelope.
    memory_block         TEXT,
    applied_heuristic_ids TEXT[],
    system_prefix_hash   TEXT,
    -- realized model + measured latency + token usage (carried from the result).
    model                TEXT,
    latency_ms           INTEGER,
    input_tokens         INTEGER,
    output_tokens        INTEGER,
    cost_usd             DOUBLE PRECISION,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Hot reads: the per-agent recent-invocations + roster aggregates filter by
-- (field_id, agent) and order by recency; run drill-downs filter by run_id.
CREATE INDEX idx_agent_invocations_field_agent_created
    ON knowledge.agent_invocations (field_id, agent, created_at DESC);
CREATE INDEX idx_agent_invocations_run_id
    ON knowledge.agent_invocations (run_id);

-- Write-ownership: coordinator-writer gets SELECT + INSERT; no DELETE is ever
-- granted, so the append-only audit-log invariant holds at the DB level (the
-- same posture as claims + belief_revisions + llm_usage). api-readonly gets
-- SELECT. No agent role is granted any write here.
GRANT SELECT, INSERT ON knowledge.agent_invocations TO mesh_writer;
GRANT SELECT ON knowledge.agent_invocations TO mesh_reader;
