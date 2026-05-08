CREATE TABLE IF NOT EXISTS beliefs (
    id VARCHAR PRIMARY KEY,
    topic VARCHAR NOT NULL,
    statement VARCHAR NOT NULL,
    supporting_claim_ids VARCHAR[] DEFAULT [],
    contradicting_claim_ids VARCHAR[] DEFAULT [],
    confidence DOUBLE NOT NULL DEFAULT 0.5,
    last_revised_at TIMESTAMPTZ NOT NULL,
    revision_count INTEGER NOT NULL DEFAULT 0,
    is_currently_held BOOLEAN NOT NULL DEFAULT TRUE
);
