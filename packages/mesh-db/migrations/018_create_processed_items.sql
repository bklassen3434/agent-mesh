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

-- Backfill from already-ingested sources so the ledger is consistent with
-- history on day one: items already in `sources` have effectively been
-- processed, so they should not be re-extracted. Idempotent (ON CONFLICT) and
-- a no-op on a fresh DB. external_id == source url, matching the coordinator's
-- _item_identity().
INSERT INTO processed_items
    (source_type, external_id, content_hash, first_seen_at, last_seen_at)
SELECT type, url, any_value(raw_content_hash), min(fetched_at), max(fetched_at)
FROM sources
GROUP BY type, url
ON CONFLICT (source_type, external_id) DO NOTHING;
