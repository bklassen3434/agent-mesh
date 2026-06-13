-- Phase 19a: belief-consolidation blocking vector.
-- Mirrors entities.name_embedding exactly: a nullable, reserved-then-populated
-- pgvector column the block→match→merge sweep needs, plus an HNSW cosine index
-- so nearest-neighbour blocking over currently-held beliefs is fast at scale.
-- The embedder (BAAI/bge-small-en-v1.5) produces cosine-normalised 384-dim
-- vectors, so vector_cosine_ops (<=> operator) is the right opclass.
ALTER TABLE knowledge.beliefs ADD COLUMN IF NOT EXISTS statement_embedding vector(384);

CREATE INDEX IF NOT EXISTS idx_beliefs_statement_embedding
    ON knowledge.beliefs
    USING hnsw (statement_embedding vector_cosine_ops);

-- DELIBERATE CONTRAST WITH 006_entity_resolution.sql: entity merge needs DELETE
-- (it removes the absorbed duplicate row), so 006 grants DELETE on entities /
-- relationships. Belief consolidation is append-only — a merged-away belief is
-- marked is_currently_held = false and keeps all its revisions for audit, never
-- deleted. mesh_writer already holds UPDATE on beliefs (005_grants.sql); that is
-- all merge, decay, and archival need. NO DELETE grant is added here, so the
-- belief-immutability / revision-append-only invariants stay enforced at the DB
-- level just as they are for claims.
