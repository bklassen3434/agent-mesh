-- Phase 12b: write-ownership enforcement via grants (docs/postgres-migration.md
-- §3). The roles themselves are created idempotently by the runner
-- (mesh_db.pg_migrations.ensure_roles) before migrations apply, so they exist
-- by the time this file runs.
--
-- mesh_writer  — coordinator / skeptic-sweep / migrations. SELECT/INSERT/UPDATE
--                but NO DELETE or TRUNCATE: backs the claim-immutability and
--                revision-append-only invariants at the DB level.
-- mesh_reader  — apps/api. SELECT only. Enforces the read-only API posture in
--                the DB, not just by convention.
--
-- This preserves the coordinator-owned-write model exactly (agents still do not
-- write directly); roles only harden what DuckDB's single-writer lock enforced.

GRANT USAGE ON SCHEMA knowledge TO mesh_writer, mesh_reader;

GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA knowledge TO mesh_writer;
GRANT SELECT ON ALL TABLES IN SCHEMA knowledge TO mesh_reader;

-- Future tables/views created by the migration owner inherit the same grants.
ALTER DEFAULT PRIVILEGES IN SCHEMA knowledge
    GRANT SELECT, INSERT, UPDATE ON TABLES TO mesh_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA knowledge
    GRANT SELECT ON TABLES TO mesh_reader;
