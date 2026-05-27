-- Phase 6a: distinguish scheduled runs from manual ones. Existing rows
-- were all manual (the scheduler didn't exist before this migration).
ALTER TABLE pipeline_runs ADD COLUMN triggered_by VARCHAR DEFAULT 'manual';
UPDATE pipeline_runs SET triggered_by = 'manual' WHERE triggered_by IS NULL;
