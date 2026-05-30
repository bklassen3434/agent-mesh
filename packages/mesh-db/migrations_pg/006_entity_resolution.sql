-- Phase 13a: entity-resolution blocking index.
-- The entities.name_embedding vector(384) column already exists (002), reserved
-- since the migration for exactly this phase; entity resolution now populates it.
-- Add an HNSW cosine index so nearest-neighbour blocking is fast at scale. The
-- embedder (BAAI/bge-small-en-v1.5) produces cosine-normalised vectors, so
-- vector_cosine_ops (<=> operator) is the right opclass.
CREATE INDEX IF NOT EXISTS idx_entities_name_embedding
    ON knowledge.entities
    USING hnsw (name_embedding vector_cosine_ops);

-- Entity merge (block→match→merge) consolidates duplicate entities onto a
-- canonical node: it re-points references (UPDATE, already granted) and then
-- removes the duplicate entity row plus any relationship edges that collapse
-- into duplicates after re-pointing. That requires DELETE — granted here ONLY
-- on the entity-identity / resolution layer (entities, relationships).
-- DELETE remains withheld on claims, beliefs, and belief_revisions, so the
-- claim-immutability and revision-append-only invariants stay enforced at the
-- DB level. Merge never touches claim content; it only re-points the FK.
GRANT DELETE ON knowledge.entities TO mesh_writer;
GRANT DELETE ON knowledge.relationships TO mesh_writer;
