CREATE TABLE IF NOT EXISTS entities (
    id VARCHAR PRIMARY KEY,
    canonical_name VARCHAR NOT NULL,
    aliases VARCHAR[] DEFAULT [],
    type VARCHAR NOT NULL,
    attributes JSON DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL
);
