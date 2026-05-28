-- Phase 7 pre-work. Structured failure classification on Skeptic-authored
-- counter-claims. Non-Skeptic claims leave this NULL. Existing rows from the
-- Skeptic agent backfill to 'other' since we cannot reconstruct the mode
-- from their free-text rationale.
ALTER TABLE claims ADD COLUMN failure_mode VARCHAR;
UPDATE claims SET failure_mode = 'other' WHERE extracted_by_agent = 'skeptic';
