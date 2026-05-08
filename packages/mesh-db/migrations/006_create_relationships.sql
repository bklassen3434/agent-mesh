CREATE TABLE IF NOT EXISTS relationships (
    id VARCHAR PRIMARY KEY,
    from_entity_id VARCHAR NOT NULL REFERENCES entities(id),
    to_entity_id VARCHAR NOT NULL REFERENCES entities(id),
    type VARCHAR NOT NULL,
    evidence_claim_ids VARCHAR[] DEFAULT [],
    confidence DOUBLE NOT NULL DEFAULT 0.5
);
