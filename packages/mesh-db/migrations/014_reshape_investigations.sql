-- Phase 7a investigation activation reshape.
-- Adds structured targeting fields (target_entity_id, hypothesis,
-- suggested_source_types, opened_by_belief_id) plus lifecycle counters
-- (pipeline_runs_attempted, collected_claim_ids) so the coordinator can
-- abandon investigations that go N runs without new evidence.
-- Renames status 'active' to 'in_progress' to match the new lifecycle vocab.

ALTER TABLE investigations ADD COLUMN target_entity_id VARCHAR;
ALTER TABLE investigations ADD COLUMN hypothesis VARCHAR;
ALTER TABLE investigations ADD COLUMN suggested_source_types VARCHAR[] DEFAULT [];
ALTER TABLE investigations ADD COLUMN opened_by_belief_id VARCHAR;
ALTER TABLE investigations ADD COLUMN pipeline_runs_attempted INTEGER DEFAULT 0;
ALTER TABLE investigations ADD COLUMN collected_claim_ids VARCHAR[] DEFAULT [];

UPDATE investigations SET status = 'in_progress' WHERE status = 'active';
