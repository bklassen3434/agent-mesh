-- Phase 12b: Postgres knowledge store.
-- pgvector for the entities.name_embedding column (latent today; entity
-- resolution populates it in a later phase). Replaces duckdb-vss.
-- The `knowledge` schema itself + the migrations bookkeeping table are
-- created by the runner (mesh_db.pg_migrations) before any file is applied.
CREATE EXTENSION IF NOT EXISTS vector;
