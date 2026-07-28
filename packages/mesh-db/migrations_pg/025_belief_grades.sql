-- Append-only accuracy grade ledger — one row per belief the accuracy eval grades
-- (supported ones too, not just failures).
--
-- Two jobs: (1) COVERAGE — the rolling "evaluate all beliefs" sweep grades the
-- least-recently-graded beliefs each pass, so it needs to know when each belief was
-- last graded; (2) ACCURACY TIME-SERIES — windowed accuracy is AVG(weight) over a
-- time range, the signal a shadow experiment's promote decision reads.
--
-- Never updated, never deleted — the full grading history stays queryable.
CREATE TABLE IF NOT EXISTS agents.belief_grades (
    id               TEXT PRIMARY KEY,
    field_id         TEXT NOT NULL REFERENCES catalog.fields(id),
    belief_id        TEXT NOT NULL,
    verdict          TEXT NOT NULL,
    judge_confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
    weight           DOUBLE PRECISION NOT NULL DEFAULT 0,
    graded_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Coverage: latest grade per belief (the rolling sweep orders by this).
CREATE INDEX IF NOT EXISTS idx_belief_grades_belief_time
    ON agents.belief_grades (field_id, belief_id, graded_at DESC);

-- Windowed accuracy: scan a field's grades since a cutoff.
CREATE INDEX IF NOT EXISTS idx_belief_grades_field_time
    ON agents.belief_grades (field_id, graded_at DESC);

GRANT SELECT, INSERT ON agents.belief_grades TO mesh_writer;
GRANT SELECT ON agents.belief_grades TO mesh_reader;
