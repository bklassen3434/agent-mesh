-- Phase 21a: full-text search over the corpus.
--
-- GIN tsvector expression indexes make beliefs/claims/entities searchable for
-- the grounded Q&A retrieval path (mesh_db.search). The index expressions are
-- IMMUTABLE (a constant 'english' regconfig + string concat/array_to_string),
-- so they qualify as expression indexes and the matching search queries in
-- mesh_db.search reuse the exact same expression to hit the index.
--
-- This is a read-only optimization: mesh_reader already holds SELECT on every
-- knowledge table (migration 005), so there is NO grant change, NO new write
-- path, and NO DELETE introduced here.

-- Builtin array_to_string is only STABLE, so it can't appear in an index
-- expression. This thin wrapper is IMMUTABLE: joining a text[] of aliases with
-- a space is deterministic. mesh_db.search references it in the matching query
-- so the planner can use the GIN index below. EXECUTE defaults to PUBLIC, so
-- mesh_reader can call it — no grant change.
CREATE OR REPLACE FUNCTION knowledge.immutable_alias_text(text[])
    RETURNS text
    LANGUAGE sql
    IMMUTABLE
    PARALLEL SAFE
    AS $$ SELECT coalesce(array_to_string($1, ' '), '') $$;

CREATE INDEX IF NOT EXISTS idx_beliefs_fts ON knowledge.beliefs
    USING GIN (to_tsvector('english', topic || ' ' || statement));

CREATE INDEX IF NOT EXISTS idx_claims_fts ON knowledge.claims
    USING GIN (to_tsvector('english', raw_excerpt));

CREATE INDEX IF NOT EXISTS idx_entities_fts ON knowledge.entities
    USING GIN (
        to_tsvector(
            'english',
            canonical_name || ' ' || knowledge.immutable_alias_text(aliases)
        )
    );
