-- Phase 24: schema reorganization — split the monolithic `knowledge` schema
-- into delineated, intuitive schemas, and pluralize the two inconsistently
-- named heuristic tables.
--
-- Until now everything lived in one `knowledge` schema: the core knowledge
-- domain, the runtime ledgers, agent memory/observability, and the field /
-- connector catalog all mixed together. This migration partitions them by
-- concern so the store reads at a glance:
--
--   knowledge — the knowledge domain proper (the immutable claims + the
--               synthesized beliefs/entities/relationships/investigations and
--               their derived signal views).
--   agents    — agent memory + observability: the heuristic head/revision log
--               and the per-skill invocation audit log.
--   runtime   — operational ledgers a run produces: pipeline_runs, llm_usage,
--               processed_items.
--   catalog   — configuration/reference data: fields, connectors, and per-field
--               connector enablement.
--
-- The split is transparent to the access layer: the pooled connection's
-- search_path now spans all four schemas (+ public), so unqualified table
-- references keep resolving exactly as before. Only schema-qualified literals
-- (the owner-connection seed inserts) and the two renamed tables touch code.
--
-- Object grants survive ALTER TABLE ... SET SCHEMA (they are per-object), so
-- mesh_writer / mesh_reader keep their existing privileges on every moved
-- table; we only add USAGE + DEFAULT PRIVILEGES on the new schemas so future
-- migrations' tables inherit the same posture migration 005 set up.

-- ── new schemas ──────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS agents;
CREATE SCHEMA IF NOT EXISTS runtime;
CREATE SCHEMA IF NOT EXISTS catalog;

GRANT USAGE ON SCHEMA agents, runtime, catalog TO mesh_writer, mesh_reader;

-- Future tables created by the migration owner in these schemas inherit the
-- same grants migration 005 set on `knowledge` (writer: SELECT/INSERT/UPDATE,
-- no DELETE; reader: SELECT).
ALTER DEFAULT PRIVILEGES IN SCHEMA agents, runtime, catalog
    GRANT SELECT, INSERT, UPDATE ON TABLES TO mesh_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA agents, runtime, catalog
    GRANT SELECT ON TABLES TO mesh_reader;

-- ── pluralize the heuristic tables (every other table is already plural) ──
ALTER TABLE knowledge.agent_heuristic RENAME TO agent_heuristics;
ALTER TABLE knowledge.agent_heuristic_revision RENAME TO agent_heuristic_revisions;

-- ── move tables into their new schemas ───────────────────────────────────
-- agents: memory + observability
ALTER TABLE knowledge.agent_heuristics SET SCHEMA agents;
ALTER TABLE knowledge.agent_heuristic_revisions SET SCHEMA agents;
ALTER TABLE knowledge.agent_invocations SET SCHEMA agents;

-- runtime: operational ledgers
ALTER TABLE knowledge.pipeline_runs SET SCHEMA runtime;
ALTER TABLE knowledge.llm_usage SET SCHEMA runtime;
ALTER TABLE knowledge.processed_items SET SCHEMA runtime;

-- catalog: configuration / reference data
ALTER TABLE knowledge.fields SET SCHEMA catalog;
ALTER TABLE knowledge.connectors SET SCHEMA catalog;
ALTER TABLE knowledge.field_connectors SET SCHEMA catalog;

-- ── align stored run_type values with the renamed pipeline jobs ──────────
-- run_type records which pipeline produced a run; keep it 1:1 with the job_id.
UPDATE runtime.pipeline_runs SET run_type = 'ingest' WHERE run_type = 'pipeline';
UPDATE runtime.pipeline_runs SET run_type = 'skeptic' WHERE run_type = 'skeptic_sweep';
UPDATE runtime.pipeline_runs SET run_type = 'memory_consolidation' WHERE run_type = 'consolidation';
