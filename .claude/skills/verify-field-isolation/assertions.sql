-- Field-isolation assertions for the Agent Mesh knowledge store, as pure SQL.
--
-- Zero-dependency fallback for the docker-only deployment (or any time the
-- Python env can't reach the DB): runs every cross-field check from
-- check_field_isolation.py through psql. Each row reports an invariant and its
-- violation count; the store is field-isolated iff every `violations` is 0.
--
--   docker compose exec -T mesh-postgres \
--     psql -U langgraph -d langgraph -f - < .claude/skills/verify-field-isolation/assertions.sql
--
-- NOTE: keep this in sync with ASSERTIONS in check_field_isolation.py.

SET search_path TO knowledge, agents, runtime, catalog, public;

WITH violations AS (
    SELECT 'claim_field_matches_subject_entity' AS invariant, count(*) AS violations
    FROM claims c JOIN entities e ON e.id = c.subject_entity_id
    WHERE c.field_id <> e.field_id

    UNION ALL
    SELECT 'claim_field_matches_source', count(*)
    FROM claims c JOIN sources s ON s.id = c.source_id
    WHERE c.field_id <> s.field_id

    UNION ALL
    SELECT 'relationship_field_matches_endpoints', count(*)
    FROM relationships r
    JOIN entities e1 ON e1.id = r.from_entity_id
    JOIN entities e2 ON e2.id = r.to_entity_id
    WHERE r.field_id <> e1.field_id OR r.field_id <> e2.field_id

    UNION ALL
    SELECT 'relationship_evidence_claim_field_matches', count(*)
    FROM relationships r, unnest(r.evidence_claim_ids) AS cid
    JOIN claims c ON c.id = cid
    WHERE r.field_id <> c.field_id

    UNION ALL
    SELECT 'belief_supporting_claim_field_matches', count(*)
    FROM beliefs b, unnest(b.supporting_claim_ids) AS cid
    JOIN claims c ON c.id = cid
    WHERE b.field_id <> c.field_id

    UNION ALL
    SELECT 'belief_contradicting_claim_field_matches', count(*)
    FROM beliefs b, unnest(b.contradicting_claim_ids) AS cid
    JOIN claims c ON c.id = cid
    WHERE b.field_id <> c.field_id

    UNION ALL
    SELECT 'investigation_field_matches_target_entity', count(*)
    FROM investigations i JOIN entities e ON e.id = i.target_entity_id
    WHERE i.target_entity_id IS NOT NULL AND i.field_id <> e.field_id

    UNION ALL
    SELECT 'investigation_field_matches_related_entities', count(*)
    FROM investigations i, unnest(i.related_entity_ids) AS eid
    JOIN entities e ON e.id = eid
    WHERE i.field_id <> e.field_id

    UNION ALL
    SELECT 'investigation_field_matches_opened_belief', count(*)
    FROM investigations i JOIN beliefs b ON b.id = i.opened_by_belief_id
    WHERE i.opened_by_belief_id IS NOT NULL AND i.field_id <> b.field_id

    UNION ALL
    SELECT 'investigation_field_matches_resolution_belief', count(*)
    FROM investigations i JOIN beliefs b ON b.id = i.resolution_belief_id
    WHERE i.resolution_belief_id IS NOT NULL AND i.field_id <> b.field_id

    UNION ALL
    SELECT 'all_field_ids_reference_a_real_field', count(*)
    FROM (
        SELECT field_id FROM claims
        WHERE NOT EXISTS (SELECT 1 FROM fields f WHERE f.id = claims.field_id)
        UNION ALL
        SELECT field_id FROM entities
        WHERE NOT EXISTS (SELECT 1 FROM fields f WHERE f.id = entities.field_id)
        UNION ALL
        SELECT field_id FROM beliefs
        WHERE NOT EXISTS (SELECT 1 FROM fields f WHERE f.id = beliefs.field_id)
    ) q
)
SELECT
    invariant,
    violations,
    CASE WHEN violations = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM violations
ORDER BY result DESC, invariant;
