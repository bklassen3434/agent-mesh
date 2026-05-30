-- Phase 12b: the seven knowledge entities, ported faithfully from the DuckDB
-- migration inventory (001-007 + the in-place ALTERs from 013, 014, 008).
-- This is the final-state schema, not a replay of the incremental history.
--
-- Faithful-port rules:
--   VARCHAR        -> TEXT
--   VARCHAR[]      -> TEXT[]            (DuckDB `DEFAULT []` -> `DEFAULT '{}'`)
--   JSON           -> JSONB
--   DOUBLE         -> DOUBLE PRECISION
--   FLOAT[384]     -> vector(384)       (pgvector)
-- Nullability/defaults match the DuckDB DDL exactly (array/json-with-default
-- columns stay nullable, as DuckDB declared them). Scalar provenance FKs
-- (claim->entity, claim->source, claim->claim, revision->belief,
-- relationship->entity, investigation->belief) are enforced. Array-valued
-- provenance (belief->claims, relationship->claims, revision->claims) is not a
-- native FK in DuckDB or Postgres and is ported as-is.

CREATE TABLE knowledge.entities (
    id             TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    aliases        TEXT[] DEFAULT '{}',
    type           TEXT NOT NULL,
    attributes     JSONB DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL,
    last_seen_at   TIMESTAMPTZ NOT NULL,
    -- VSS column, intentionally unpopulated until the entity-resolution phase
    -- (was FLOAT[384] under duckdb-vss).
    name_embedding vector(384)
);

CREATE TABLE knowledge.sources (
    id                TEXT PRIMARY KEY,
    type              TEXT NOT NULL,
    url               TEXT NOT NULL,
    author            TEXT,
    published_at      TIMESTAMPTZ NOT NULL,
    fetched_at        TIMESTAMPTZ NOT NULL,
    raw_content_hash  TEXT NOT NULL,
    reliability_prior DOUBLE PRECISION NOT NULL DEFAULT 0.5
);

CREATE TABLE knowledge.claims (
    id                     TEXT PRIMARY KEY,
    predicate              TEXT NOT NULL,
    subject_entity_id      TEXT NOT NULL REFERENCES knowledge.entities(id),
    object                 JSONB NOT NULL,
    source_id              TEXT NOT NULL REFERENCES knowledge.sources(id),
    extracted_at           TIMESTAMPTZ NOT NULL,
    extracted_by_agent     TEXT NOT NULL,
    raw_excerpt            TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'active',
    confidence             DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    superseded_by_claim_id TEXT REFERENCES knowledge.claims(id),
    -- Phase 7 (migration 013): structured failure mode on Skeptic counter-claims.
    failure_mode           TEXT
);

CREATE TABLE knowledge.beliefs (
    id                      TEXT PRIMARY KEY,
    topic                   TEXT NOT NULL,
    statement               TEXT NOT NULL,
    supporting_claim_ids    TEXT[] DEFAULT '{}',
    contradicting_claim_ids TEXT[] DEFAULT '{}',
    confidence              DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    last_revised_at         TIMESTAMPTZ NOT NULL,
    revision_count          INTEGER NOT NULL DEFAULT 0,
    is_currently_held       BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE knowledge.belief_revisions (
    id                  TEXT PRIMARY KEY,
    belief_id           TEXT NOT NULL REFERENCES knowledge.beliefs(id),
    previous_statement  TEXT NOT NULL,
    new_statement       TEXT NOT NULL,
    previous_confidence DOUBLE PRECISION NOT NULL,
    new_confidence      DOUBLE PRECISION NOT NULL,
    trigger_claim_ids   TEXT[] DEFAULT '{}',
    revised_by_agent    TEXT NOT NULL,
    revised_at          TIMESTAMPTZ NOT NULL,
    rationale           TEXT NOT NULL
);

CREATE TABLE knowledge.relationships (
    id                 TEXT PRIMARY KEY,
    from_entity_id     TEXT NOT NULL REFERENCES knowledge.entities(id),
    to_entity_id       TEXT NOT NULL REFERENCES knowledge.entities(id),
    type               TEXT NOT NULL,
    evidence_claim_ids TEXT[] DEFAULT '{}',
    confidence         DOUBLE PRECISION NOT NULL DEFAULT 0.5
);

CREATE TABLE knowledge.investigations (
    id                      TEXT PRIMARY KEY,
    question                TEXT NOT NULL,
    related_entity_ids      TEXT[] DEFAULT '{}',
    status                  TEXT NOT NULL DEFAULT 'open',
    priority                DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    created_at              TIMESTAMPTZ NOT NULL,
    resolved_at             TIMESTAMPTZ,
    resolution_belief_id    TEXT REFERENCES knowledge.beliefs(id),
    assigned_scout_agents   TEXT[] DEFAULT '{}',
    -- Phase 7a (migration 014). DuckDB added these via ALTER without FKs, so
    -- target_entity_id / opened_by_belief_id are plain columns here too.
    target_entity_id        TEXT,
    hypothesis              TEXT,
    suggested_source_types  TEXT[] DEFAULT '{}',
    opened_by_belief_id     TEXT,
    pipeline_runs_attempted INTEGER DEFAULT 0,
    collected_claim_ids     TEXT[] DEFAULT '{}'
);

-- Indexes backing the hot join/filter paths the access layer + views use.
CREATE INDEX idx_claims_source_id ON knowledge.claims (source_id);
CREATE INDEX idx_claims_subject_entity_id ON knowledge.claims (subject_entity_id);
CREATE INDEX idx_claims_extracted_at ON knowledge.claims (extracted_at);
CREATE INDEX idx_claims_status ON knowledge.claims (status);
CREATE INDEX idx_beliefs_currently_held ON knowledge.beliefs (is_currently_held);
CREATE INDEX idx_belief_revisions_belief_id ON knowledge.belief_revisions (belief_id);
CREATE INDEX idx_relationships_from ON knowledge.relationships (from_entity_id);
CREATE INDEX idx_relationships_to ON knowledge.relationships (to_entity_id);
