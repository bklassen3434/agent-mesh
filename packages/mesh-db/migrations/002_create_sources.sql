CREATE TABLE IF NOT EXISTS sources (
    id VARCHAR PRIMARY KEY,
    type VARCHAR NOT NULL,
    url VARCHAR NOT NULL,
    author VARCHAR,
    published_at TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    raw_content_hash VARCHAR NOT NULL,
    reliability_prior DOUBLE NOT NULL DEFAULT 0.5
);
