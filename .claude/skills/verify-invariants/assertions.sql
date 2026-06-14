-- Invariant assertions for the Agent Mesh knowledge store, as pure SQL.
--
-- Zero-dependency fallback for the docker-only deployment (or any time the
-- Python env can't reach the DB): runs every invariant from check_invariants.py
-- through psql. Each row reports an invariant and its violation count; the store
-- is consistent iff every `violations` is 0.
--
--   docker compose exec -T mesh-postgres \
--     psql -U langgraph -d langgraph -f - < .claude/skills/verify-invariants/assertions.sql
--
-- (or pipe into psql inside any container/host that can reach the store).
-- NOTE: keep this in sync with ASSERTIONS in check_invariants.py.

SET search_path TO knowledge, agents, runtime, catalog, public;

WITH violations AS (
    SELECT 'claim_supersession_pointer' AS invariant, count(*) AS violations
    FROM claims
    WHERE (status = 'superseded' AND superseded_by_claim_id IS NULL)
       OR (superseded_by_claim_id = id)

    UNION ALL
    SELECT 'revision_count_matches_rows', count(*) FROM (
        SELECT b.id
        FROM beliefs b
        LEFT JOIN belief_revisions r ON r.belief_id = b.id
        GROUP BY b.id, b.revision_count
        HAVING b.revision_count <> count(r.id)
    ) q

    UNION ALL
    SELECT 'belief_supporting_claims_exist', count(*)
    FROM beliefs b, unnest(b.supporting_claim_ids) AS cid
    WHERE NOT EXISTS (SELECT 1 FROM claims c WHERE c.id = cid)

    UNION ALL
    SELECT 'belief_contradicting_claims_exist', count(*)
    FROM beliefs b, unnest(b.contradicting_claim_ids) AS cid
    WHERE NOT EXISTS (SELECT 1 FROM claims c WHERE c.id = cid)

    UNION ALL
    SELECT 'revision_trigger_claims_exist', count(*)
    FROM belief_revisions r, unnest(r.trigger_claim_ids) AS cid
    WHERE NOT EXISTS (SELECT 1 FROM claims c WHERE c.id = cid)

    UNION ALL
    SELECT 'relationship_evidence_claims_exist', count(*)
    FROM relationships rel, unnest(rel.evidence_claim_ids) AS cid
    WHERE NOT EXISTS (SELECT 1 FROM claims c WHERE c.id = cid)

    UNION ALL
    SELECT 'no_self_relationships', count(*)
    FROM relationships
    WHERE from_entity_id = to_entity_id

    UNION ALL
    SELECT 'held_belief_has_support', count(*)
    FROM beliefs
    WHERE is_currently_held
      AND cardinality(coalesce(supporting_claim_ids, '{}')) = 0

    UNION ALL
    SELECT 'claim_type_matches_predicate', count(*)
    FROM claims c
    LEFT JOIN (
        VALUES
            ('achieves_score', 'score'),
            ('outperforms', 'comparison'),
            ('developed_by', 'attribution'),
            ('evaluated_on', 'evaluation'),
            ('has_capability', 'capability'),
            ('based_on', 'lineage'),
            ('reproduces', 'reproduction'),
            ('critiques', 'critique'),
            ('speculates', 'speculative')
    ) AS expected(predicate, claim_type) ON expected.predicate = c.predicate
    WHERE (expected.claim_type IS NOT NULL AND c.claim_type <> expected.claim_type)
       OR (expected.claim_type IS NULL AND c.claim_type <> 'speculative')
)
SELECT
    invariant,
    violations,
    CASE WHEN violations = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM violations
ORDER BY result DESC, invariant;
