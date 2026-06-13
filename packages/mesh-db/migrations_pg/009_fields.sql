-- Phase 17a: first-class Field scope + universal field_id partitioning.
--
-- A Field scopes everything that is field-knowledge or field-learned state:
-- entities, sources, claims, beliefs, relationships, investigations,
-- agent_heuristic, pipeline_runs, processed_items (PK extended), and schedules
-- (handled in mesh_a2a.schedules, public schema). field_id is a PARTITION, not a
-- content axis: the engine never branches on it; it only scopes reads/writes.
--
-- belief_revisions and agent_heuristic_revision inherit scope through their head
-- FK (belief_id / heuristic_id) and get no column. llm_usage inherits field via
-- run_id (join). The connector catalog is global; per-field connector enablement
-- arrives in 17c.
--
-- The seeded ai-robotics field carries a minimal placeholder profile here; the
-- full canonical FieldProfile is upserted by init_pg (Python is the source of
-- truth, since the few-shot text contains characters this naive SQL runner cannot
-- carry safely). Every pre-existing row is backfilled into ai-robotics before the
-- NOT NULL / FK constraints land.

CREATE TABLE knowledge.fields (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    slug       TEXT UNIQUE NOT NULL,
    profile    JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active  BOOLEAN NOT NULL DEFAULT TRUE
);

INSERT INTO knowledge.fields (id, name, slug, profile)
VALUES (
    'ai-robotics',
    'AI & Robotics',
    'ai-robotics',
    '{"slug": "ai-robotics", "name": "AI & Robotics", "description": "an AI/robotics research knowledge base", "entity_type_hints": [], "extraction_examples": "", "topic_label": "sota"}'::jsonb
);

-- entities
ALTER TABLE knowledge.entities ADD COLUMN field_id TEXT;
UPDATE knowledge.entities SET field_id = 'ai-robotics';
ALTER TABLE knowledge.entities ALTER COLUMN field_id SET NOT NULL;
ALTER TABLE knowledge.entities
    ADD CONSTRAINT entities_field_id_fkey
    FOREIGN KEY (field_id) REFERENCES knowledge.fields(id);

-- sources
ALTER TABLE knowledge.sources ADD COLUMN field_id TEXT;
UPDATE knowledge.sources SET field_id = 'ai-robotics';
ALTER TABLE knowledge.sources ALTER COLUMN field_id SET NOT NULL;
ALTER TABLE knowledge.sources
    ADD CONSTRAINT sources_field_id_fkey
    FOREIGN KEY (field_id) REFERENCES knowledge.fields(id);

-- claims
ALTER TABLE knowledge.claims ADD COLUMN field_id TEXT;
UPDATE knowledge.claims SET field_id = 'ai-robotics';
ALTER TABLE knowledge.claims ALTER COLUMN field_id SET NOT NULL;
ALTER TABLE knowledge.claims
    ADD CONSTRAINT claims_field_id_fkey
    FOREIGN KEY (field_id) REFERENCES knowledge.fields(id);

-- beliefs
ALTER TABLE knowledge.beliefs ADD COLUMN field_id TEXT;
UPDATE knowledge.beliefs SET field_id = 'ai-robotics';
ALTER TABLE knowledge.beliefs ALTER COLUMN field_id SET NOT NULL;
ALTER TABLE knowledge.beliefs
    ADD CONSTRAINT beliefs_field_id_fkey
    FOREIGN KEY (field_id) REFERENCES knowledge.fields(id);

-- relationships
ALTER TABLE knowledge.relationships ADD COLUMN field_id TEXT;
UPDATE knowledge.relationships SET field_id = 'ai-robotics';
ALTER TABLE knowledge.relationships ALTER COLUMN field_id SET NOT NULL;
ALTER TABLE knowledge.relationships
    ADD CONSTRAINT relationships_field_id_fkey
    FOREIGN KEY (field_id) REFERENCES knowledge.fields(id);

-- investigations
ALTER TABLE knowledge.investigations ADD COLUMN field_id TEXT;
UPDATE knowledge.investigations SET field_id = 'ai-robotics';
ALTER TABLE knowledge.investigations ALTER COLUMN field_id SET NOT NULL;
ALTER TABLE knowledge.investigations
    ADD CONSTRAINT investigations_field_id_fkey
    FOREIGN KEY (field_id) REFERENCES knowledge.fields(id);

-- agent_heuristic
ALTER TABLE knowledge.agent_heuristic ADD COLUMN field_id TEXT;
UPDATE knowledge.agent_heuristic SET field_id = 'ai-robotics';
ALTER TABLE knowledge.agent_heuristic ALTER COLUMN field_id SET NOT NULL;
ALTER TABLE knowledge.agent_heuristic
    ADD CONSTRAINT agent_heuristic_field_id_fkey
    FOREIGN KEY (field_id) REFERENCES knowledge.fields(id);

-- pipeline_runs
ALTER TABLE knowledge.pipeline_runs ADD COLUMN field_id TEXT;
UPDATE knowledge.pipeline_runs SET field_id = 'ai-robotics';
ALTER TABLE knowledge.pipeline_runs ALTER COLUMN field_id SET NOT NULL;
ALTER TABLE knowledge.pipeline_runs
    ADD CONSTRAINT pipeline_runs_field_id_fkey
    FOREIGN KEY (field_id) REFERENCES knowledge.fields(id);

-- processed_items: the dedup ledger is keyed (source_type, external_id); extend
-- the PK to include field_id so the same external source is ingested
-- independently per field.
ALTER TABLE knowledge.processed_items ADD COLUMN field_id TEXT;
UPDATE knowledge.processed_items SET field_id = 'ai-robotics';
ALTER TABLE knowledge.processed_items ALTER COLUMN field_id SET NOT NULL;
ALTER TABLE knowledge.processed_items
    ADD CONSTRAINT processed_items_field_id_fkey
    FOREIGN KEY (field_id) REFERENCES knowledge.fields(id);
ALTER TABLE knowledge.processed_items DROP CONSTRAINT processed_items_pkey;
ALTER TABLE knowledge.processed_items
    ADD PRIMARY KEY (field_id, source_type, external_id);

-- field_id-leading composite indexes on the hot filter paths.
CREATE INDEX idx_entities_field_type ON knowledge.entities (field_id, type);
CREATE INDEX idx_claims_field_subject ON knowledge.claims (field_id, subject_entity_id);
CREATE INDEX idx_claims_field_status ON knowledge.claims (field_id, status);
CREATE INDEX idx_beliefs_field_held ON knowledge.beliefs (field_id, is_currently_held);
CREATE INDEX idx_agent_heuristic_field_scope
    ON knowledge.agent_heuristic (field_id, agent, skill, is_currently_active);

-- Derived views carry field_id as a passthrough (a belief belongs to exactly one
-- field; the column never changes the aggregation, only lets readers scope).
-- CREATE OR REPLACE appends field_id as the final column.
CREATE OR REPLACE VIEW knowledge.belief_reproduction AS
WITH belief_claim_links AS (
    SELECT id AS belief_id, unnest(supporting_claim_ids) AS claim_id
    FROM knowledge.beliefs WHERE is_currently_held = TRUE
    UNION ALL
    SELECT id AS belief_id, unnest(contradicting_claim_ids) AS claim_id
    FROM knowledge.beliefs WHERE is_currently_held = TRUE
),
canonical AS (
    SELECT
        bcl.belief_id,
        c.predicate,
        c.subject_entity_id,
        CASE
            WHEN c.predicate IN ('achieves_score', 'outperforms', 'evaluated_on')
                 AND c.object->>'benchmark' IS NOT NULL
                 AND c.object->'score' IS NOT NULL
            THEN 'benchmark=' || lower(c.object->>'benchmark') || '|score='
                 || to_char((c.object->>'score')::double precision, 'FM999999990.0')
            WHEN c.predicate = 'developed_by' AND c.object->>'organization' IS NOT NULL
            THEN 'org=' || lower(c.object->>'organization')
            ELSE c.object::text
        END AS object_key,
        s.type AS source_type
    FROM belief_claim_links bcl
    JOIN knowledge.claims c ON c.id = bcl.claim_id
    JOIN knowledge.sources s ON s.id = c.source_id
),
per_canonical AS (
    SELECT
        belief_id, predicate, subject_entity_id, object_key,
        COUNT(DISTINCT source_type) AS distinct_source_types
    FROM canonical
    GROUP BY belief_id, predicate, subject_entity_id, object_key
)
SELECT
    b.id AS belief_id,
    COALESCE(MAX(pc.distinct_source_types), 0) AS reproduction_count,
    b.field_id
FROM knowledge.beliefs b
LEFT JOIN per_canonical pc ON pc.belief_id = b.id
WHERE b.is_currently_held = TRUE
GROUP BY b.id;

CREATE OR REPLACE VIEW knowledge.belief_signals AS
WITH source_diversity AS (
    SELECT b.id AS belief_id, COUNT(DISTINCT s.type) AS source_types
    FROM knowledge.beliefs b
    LEFT JOIN (
        SELECT id AS belief_id, unnest(supporting_claim_ids) AS claim_id
        FROM knowledge.beliefs WHERE is_currently_held = TRUE
    ) bcl ON bcl.belief_id = b.id
    LEFT JOIN knowledge.claims c ON c.id = bcl.claim_id
    LEFT JOIN knowledge.sources s ON s.id = c.source_id
    WHERE b.is_currently_held = TRUE
    GROUP BY b.id
),
skeptic_attacks AS (
    SELECT
        b.id AS belief_id,
        COUNT(c.id) AS skeptic_counter_claim_count,
        SUM(
            CASE WHEN c.failure_mode IN (
                'methodological_flaw', 'cherry_picked_evidence', 'contradicted_by_source'
            ) THEN 1 ELSE 0 END
        ) AS severe_failure_mode_count
    FROM knowledge.beliefs b
    LEFT JOIN (
        SELECT id AS belief_id, unnest(contradicting_claim_ids) AS claim_id
        FROM knowledge.beliefs WHERE is_currently_held = TRUE
    ) bcl ON bcl.belief_id = b.id
    LEFT JOIN knowledge.claims c
        ON c.id = bcl.claim_id AND c.extracted_by_agent = 'skeptic'
    WHERE b.is_currently_held = TRUE
    GROUP BY b.id
),
claim_velocity AS (
    SELECT b.id AS belief_id, COUNT(c.id) AS claims_last_30d
    FROM knowledge.beliefs b
    LEFT JOIN (
        SELECT id AS belief_id, unnest(supporting_claim_ids) AS claim_id
        FROM knowledge.beliefs WHERE is_currently_held = TRUE
    ) bcl ON bcl.belief_id = b.id
    LEFT JOIN knowledge.claims c
        ON c.id = bcl.claim_id AND c.extracted_at > (now() - INTERVAL '30 days')
    WHERE b.is_currently_held = TRUE
    GROUP BY b.id
)
SELECT
    b.id AS belief_id,
    COALESCE(sd.source_types, 0) AS source_type_diversity,
    COALESCE(br.reproduction_count, 0) AS reproduction_count,
    COALESCE(sa.skeptic_counter_claim_count, 0) AS skeptic_counter_claim_count,
    COALESCE(sa.severe_failure_mode_count, 0) AS severe_failure_mode_count,
    COALESCE(cv.claims_last_30d, 0) AS claims_last_30d,
    b.field_id
FROM knowledge.beliefs b
LEFT JOIN source_diversity sd ON sd.belief_id = b.id
LEFT JOIN knowledge.belief_reproduction br ON br.belief_id = b.id
LEFT JOIN skeptic_attacks sa ON sa.belief_id = b.id
LEFT JOIN claim_velocity cv ON cv.belief_id = b.id
WHERE b.is_currently_held = TRUE;

CREATE OR REPLACE VIEW knowledge.belief_hype_substance AS
SELECT
    belief_id,
    source_type_diversity,
    reproduction_count,
    skeptic_counter_claim_count,
    severe_failure_mode_count,
    claims_last_30d,
    GREATEST(
        0.0,
        LEAST(
            1.0,
            0.5
            + 0.5 * (
                LEAST(source_type_diversity / 4.0, 1.0)
                + LEAST(reproduction_count / 3.0, 1.0)
            ) / 2.0
            - 0.5 * (
                LEAST(skeptic_counter_claim_count / 4.0, 1.0)
                + LEAST(severe_failure_mode_count / 3.0, 1.0)
            ) / 2.0
        )
    ) AS hype_substance_score,
    field_id
FROM knowledge.belief_signals;

-- Grants: writer writes fields, reader reads (no DELETE: fields are deactivated,
-- not removed). The ALTER DEFAULT PRIVILEGES from 005 also covers this; explicit
-- here keeps intent legible.
GRANT SELECT, INSERT, UPDATE ON knowledge.fields TO mesh_writer;
GRANT SELECT ON knowledge.fields TO mesh_reader;
