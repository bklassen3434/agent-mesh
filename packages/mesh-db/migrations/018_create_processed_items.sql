-- Phase 11b: processed-items ledger for dedup-before-extraction. One row per
-- logical scouted item, keyed on (source_type, external_id). The coordinator
-- consults this before dispatching extract_claims: an unseen item is extracted,
-- a seen item with an unchanged content_hash is skipped (no LLM call), and a
-- seen item whose content_hash changed is re-extracted. Coordinator-owned
-- writes only (DuckDB single-writer).
CREATE TABLE IF NOT EXISTS processed_items (
    source_type VARCHAR NOT NULL,
    external_id VARCHAR NOT NULL,
    content_hash VARCHAR NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (source_type, external_id)
);
