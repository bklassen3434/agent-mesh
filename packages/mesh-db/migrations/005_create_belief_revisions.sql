CREATE TABLE IF NOT EXISTS belief_revisions (
    id VARCHAR PRIMARY KEY,
    belief_id VARCHAR NOT NULL REFERENCES beliefs(id),
    previous_statement VARCHAR NOT NULL,
    new_statement VARCHAR NOT NULL,
    previous_confidence DOUBLE NOT NULL,
    new_confidence DOUBLE NOT NULL,
    trigger_claim_ids VARCHAR[] DEFAULT [],
    revised_by_agent VARCHAR NOT NULL,
    revised_at TIMESTAMPTZ NOT NULL,
    rationale VARCHAR NOT NULL
);
