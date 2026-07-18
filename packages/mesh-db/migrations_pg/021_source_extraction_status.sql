-- Terminal state for extract-source: "attempted, produced nothing".
--
-- The unextracted_source tension was inferred purely from "this source has no
-- claim rows" (a NOT EXISTS anti-join). A source that legitimately yields zero
-- claims — off-topic HN noise, an index page, an off-topic paper — therefore
-- satisfied that condition forever: the tension re-fired every sensing pass,
-- stalled, escalated to a 3x swarm, and re-ran extraction on the same dead
-- sources every round. On the free-tier LLM this consumed the entire daily
-- token budget on ~19 un-extractable sources, starving real ingestion.
--
-- extraction_status records that the reader tried and there was nothing to
-- pull; unextracted_sources then excludes it. extraction_attempts bounds
-- transient parse-failure retries (a paper that always fails to parse is
-- retired after MAX_EXTRACTION_ATTEMPTS rather than churning forever).
-- Additive + defaulted: existing rows are 'pending' with 0 attempts, so the
-- first pass re-derives their real state (claims → excluded; empty → exhausted).
ALTER TABLE knowledge.sources
    ADD COLUMN IF NOT EXISTS extraction_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE knowledge.sources
    ADD COLUMN IF NOT EXISTS extraction_attempts INTEGER NOT NULL DEFAULT 0;

-- The unextracted_sources anti-join filters on (field_id, extraction_status).
CREATE INDEX IF NOT EXISTS idx_sources_field_extraction_status
    ON knowledge.sources (field_id, extraction_status);
