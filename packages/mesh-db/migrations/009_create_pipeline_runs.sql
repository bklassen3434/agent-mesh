CREATE TABLE IF NOT EXISTS pipeline_runs (
    id VARCHAR PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    papers_scouted INTEGER DEFAULT 0,
    sources_inserted INTEGER DEFAULT 0,
    claims_inserted INTEGER DEFAULT 0,
    entities_created INTEGER DEFAULT 0,
    beliefs_created INTEGER DEFAULT 0,
    beliefs_revised INTEGER DEFAULT 0,
    avg_extraction_latency_ms INTEGER DEFAULT 0,
    errors JSON DEFAULT '[]'
);
