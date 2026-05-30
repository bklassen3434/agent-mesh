-- Phase 12b: Phase 7b derived-signal views, ported from DuckDB migration 015.
-- Plain views (recomputed on read) per the 12a decision — fast enough at
-- representative volume; matview promotion held in reserve (see
-- docs/postgres-migration.md §6). DuckDB-ism translations:
--   UNNEST(arr)              -> unnest(arr)
--   json_extract_string(o,$.k) -> o->>'k'      json_extract(o,$.k) -> o->'k'
--   cast(o AS VARCHAR)       -> o::text
--   printf('%.1f', x)        -> to_char(x::double precision, 'FM999999990.0')
--   now() - INTERVAL 30 DAY  -> now() - INTERVAL '30 days'

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
    COALESCE(MAX(pc.distinct_source_types), 0) AS reproduction_count
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
    COALESCE(cv.claims_last_30d, 0) AS claims_last_30d
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
    ) AS hype_substance_score
FROM knowledge.belief_signals;
