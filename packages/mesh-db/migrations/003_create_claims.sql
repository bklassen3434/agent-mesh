CREATE TABLE IF NOT EXISTS claims (
    id VARCHAR PRIMARY KEY,
    predicate VARCHAR NOT NULL,
    subject_entity_id VARCHAR NOT NULL REFERENCES entities(id),
    object JSON NOT NULL,
    source_id VARCHAR NOT NULL REFERENCES sources(id),
    extracted_at TIMESTAMPTZ NOT NULL,
    extracted_by_agent VARCHAR NOT NULL,
    raw_excerpt VARCHAR NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'active',
    confidence DOUBLE NOT NULL DEFAULT 0.5,
    superseded_by_claim_id VARCHAR REFERENCES claims(id)
);
