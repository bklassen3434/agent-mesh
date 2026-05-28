-- Phase 7b derived signal views.
-- Views, not stored columns — recomputed on read. The signals are
-- informational only in 7b and do not drive any mesh behavior.

-- belief_reproduction
-- For each currently-held belief, how many distinct source types back
-- any single canonical claim attached to it. Canonical = predicate +
-- subject + a coarse object-key matching the rule documented in
-- docs/reproduction-signal-exploration.md (rounded numeric scores,
-- lowercased benchmark names, lowercased org names, raw json otherwise).
CREATE OR REPLACE VIEW belief_reproduction AS
WITH belief_claim_links AS (
    SELECT id AS belief_id, UNNEST(supporting_claim_ids) AS claim_id
    FROM beliefs WHERE is_currently_held = TRUE
    UNION ALL
    SELECT id AS belief_id, UNNEST(contradicting_claim_ids) AS claim_id
    FROM beliefs WHERE is_currently_held = TRUE
),
canonical AS (
    SELECT
        bcl.belief_id,
        c.predicate,
        c.subject_entity_id,
        CASE
            WHEN c.predicate IN ('achieves_score', 'outperforms', 'evaluated_on')
                 AND json_extract_string(c.object, '$.benchmark') IS NOT NULL
                 AND json_extract(c.object, '$.score') IS NOT NULL
            THEN concat(
                'benchmark=',
                lower(json_extract_string(c.object, '$.benchmark')),
                '|score=',
                printf('%.1f', cast(json_extract_string(c.object, '$.score') AS DOUBLE))
            )
            WHEN c.predicate = 'developed_by'
                 AND json_extract_string(c.object, '$.organization') IS NOT NULL
            THEN concat('org=', lower(json_extract_string(c.object, '$.organization')))
            ELSE cast(c.object AS VARCHAR)
        END AS object_key,
        s.type AS source_type
    FROM belief_claim_links bcl
    JOIN claims c ON c.id = bcl.claim_id
    JOIN sources s ON s.id = c.source_id
),
per_canonical AS (
    SELECT
        belief_id,
        predicate,
        subject_entity_id,
        object_key,
        COUNT(DISTINCT source_type) AS distinct_source_types
    FROM canonical
    GROUP BY belief_id, predicate, subject_entity_id, object_key
)
SELECT
    b.id AS belief_id,
    COALESCE(MAX(pc.distinct_source_types), 0) AS reproduction_count
FROM beliefs b
LEFT JOIN per_canonical pc ON pc.belief_id = b.id
WHERE b.is_currently_held = TRUE
GROUP BY b.id;


-- belief_signals
-- The raw inputs feeding the hype/substance score. Exposed as its own
-- view so the API can return individual signals for transparency, not
-- just the aggregate number.
CREATE OR REPLACE VIEW belief_signals AS
WITH source_diversity AS (
    SELECT
        b.id AS belief_id,
        COUNT(DISTINCT s.type) AS source_types
    FROM beliefs b
    LEFT JOIN (
        SELECT id AS belief_id, UNNEST(supporting_claim_ids) AS claim_id
        FROM beliefs WHERE is_currently_held = TRUE
    ) bcl ON bcl.belief_id = b.id
    LEFT JOIN claims c ON c.id = bcl.claim_id
    LEFT JOIN sources s ON s.id = c.source_id
    WHERE b.is_currently_held = TRUE
    GROUP BY b.id
),
skeptic_attacks AS (
    SELECT
        b.id AS belief_id,
        COUNT(c.id) AS skeptic_counter_claim_count,
        SUM(
            CASE
                WHEN c.failure_mode IN (
                    'methodological_flaw',
                    'cherry_picked_evidence',
                    'contradicted_by_source'
                ) THEN 1 ELSE 0
            END
        ) AS severe_failure_mode_count
    FROM beliefs b
    LEFT JOIN (
        SELECT id AS belief_id, UNNEST(contradicting_claim_ids) AS claim_id
        FROM beliefs WHERE is_currently_held = TRUE
    ) bcl ON bcl.belief_id = b.id
    LEFT JOIN claims c
        ON c.id = bcl.claim_id AND c.extracted_by_agent = 'skeptic'
    WHERE b.is_currently_held = TRUE
    GROUP BY b.id
),
claim_velocity AS (
    SELECT
        b.id AS belief_id,
        COUNT(c.id) AS claims_last_30d
    FROM beliefs b
    LEFT JOIN (
        SELECT id AS belief_id, UNNEST(supporting_claim_ids) AS claim_id
        FROM beliefs WHERE is_currently_held = TRUE
    ) bcl ON bcl.belief_id = b.id
    LEFT JOIN claims c
        ON c.id = bcl.claim_id
        AND c.extracted_at > (now() - INTERVAL 30 DAY)
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
FROM beliefs b
LEFT JOIN source_diversity sd ON sd.belief_id = b.id
LEFT JOIN belief_reproduction br ON br.belief_id = b.id
LEFT JOIN skeptic_attacks sa ON sa.belief_id = b.id
LEFT JOIN claim_velocity cv ON cv.belief_id = b.id
WHERE b.is_currently_held = TRUE;


-- belief_hype_substance
-- Single 0-1 score per belief combining the signals. Higher = more
-- substantive, lower = more hype-shaped. Formula documented in
-- docs/derived-signals.md.
--
-- substance = avg(source_diversity_norm, reproduction_norm) * 0.5
-- hype      = avg(attack_count_norm, severe_failure_norm)   * 0.5
-- final     = clamp(0.5 + substance - hype, 0, 1)
--
-- The 0.5 anchor means a belief with zero supporting AND zero attacking
-- evidence sits at 0.5 — informational sweet spot, neither
-- substantive nor hype. Equal weighting keeps the formula symmetric.
CREATE OR REPLACE VIEW belief_hype_substance AS
SELECT
    belief_id,
    -- exposed so callers can inspect, not just the final number
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
FROM belief_signals;
