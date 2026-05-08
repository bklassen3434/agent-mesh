CREATE TABLE IF NOT EXISTS investigations (
    id VARCHAR PRIMARY KEY,
    question VARCHAR NOT NULL,
    related_entity_ids VARCHAR[] DEFAULT [],
    status VARCHAR NOT NULL DEFAULT 'open',
    priority DOUBLE NOT NULL DEFAULT 0.5,
    created_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ,
    resolution_belief_id VARCHAR REFERENCES beliefs(id),
    assigned_scout_agents VARCHAR[] DEFAULT []
);
