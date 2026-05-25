-- Distinguish main-pipeline runs from skeptic-sweep runs (and future job types)
-- so /pipeline-runs queries and stats can filter cleanly. DuckDB rejects
-- NOT NULL on ALTER TABLE ADD COLUMN, so the column is nullable at the schema
-- level. The Python code always sets it, and the DEFAULT backfills old rows.
ALTER TABLE pipeline_runs ADD COLUMN run_type VARCHAR DEFAULT 'pipeline';
UPDATE pipeline_runs SET run_type = 'pipeline' WHERE run_type IS NULL;
