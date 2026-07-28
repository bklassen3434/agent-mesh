-- Shadow A/B experiments — a candidate change tested beside the live pipeline.
--
-- improve-component opens one (status='running') with a candidate variant; a
-- shadow-eval skill runs both arms on the same real inputs and accumulates each
-- arm's graded scores here (running sums); a decide rule promotes the candidate
-- only once both arms have min_sample samples AND treatment beats control by the
-- margin. The candidate's output is never written to the KB — only its scores land
-- here. Rows are kept after a decision (audit); status flips running→promoted/rejected.
CREATE TABLE IF NOT EXISTS agents.improvement_experiments (
    id                  TEXT PRIMARY KEY,
    field_id            TEXT NOT NULL REFERENCES catalog.fields(id),
    component           TEXT NOT NULL,
    target              TEXT NOT NULL DEFAULT '',
    treatment_prompt    TEXT NOT NULL DEFAULT '',
    concern_ids         TEXT[] NOT NULL DEFAULT '{}',
    status              TEXT NOT NULL DEFAULT 'running',
    control_n           INTEGER NOT NULL DEFAULT 0,
    control_score_sum   DOUBLE PRECISION NOT NULL DEFAULT 0,
    treatment_n         INTEGER NOT NULL DEFAULT 0,
    treatment_score_sum DOUBLE PRECISION NOT NULL DEFAULT 0,
    min_sample          INTEGER NOT NULL DEFAULT 20,
    margin              DOUBLE PRECISION NOT NULL DEFAULT 0.02,
    rationale           TEXT NOT NULL DEFAULT '',
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at          TIMESTAMPTZ
);

-- At most one running experiment per (field, component, target).
CREATE UNIQUE INDEX IF NOT EXISTS uq_improvement_experiments_running
    ON agents.improvement_experiments (field_id, component, target)
    WHERE status = 'running';

CREATE INDEX IF NOT EXISTS idx_improvement_experiments_field_status
    ON agents.improvement_experiments (field_id, status);

GRANT SELECT, INSERT, UPDATE ON agents.improvement_experiments TO mesh_writer;
GRANT SELECT ON agents.improvement_experiments TO mesh_reader;
