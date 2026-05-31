-- Phase 14a: type the claims.
--
-- Every claim gains a derived `claim_type` — the routing key the generalized
-- synthesis step (14b/c) dispatches on. It is classification metadata derived
-- 1:1 from the predicate (see mesh_models.claim.PREDICATE_TO_CLAIM_TYPE); it
-- does NOT alter claim content, so claim immutability is preserved.
--
-- Backfill is deterministic: every pre-existing claim carries one of the four
-- legacy predicates, which map cleanly to a claim_type. The new types
-- (capability/lineage/reproduction/critique/speculative) can only arrive from
-- the Phase-14a extractor, so there is nothing for an LLM pass to discover in
-- already-stored claims — a CASE map is exact and free.

ALTER TABLE knowledge.claims ADD COLUMN IF NOT EXISTS claim_type TEXT;

-- Deterministic backfill (idempotent: only fills NULLs). The ELSE mirrors the
-- model's fallback — unknown predicates land in the inert `speculative` bucket
-- (14b never synthesizes it), so a surprise predicate can't mint a belief.
UPDATE knowledge.claims
SET claim_type = CASE predicate
    WHEN 'achieves_score' THEN 'score'
    WHEN 'outperforms'    THEN 'comparison'
    WHEN 'developed_by'   THEN 'attribution'
    WHEN 'evaluated_on'   THEN 'evaluation'
    WHEN 'has_capability' THEN 'capability'
    WHEN 'based_on'       THEN 'lineage'
    WHEN 'reproduces'     THEN 'reproduction'
    WHEN 'critiques'      THEN 'critique'
    WHEN 'speculates'     THEN 'speculative'
    ELSE 'speculative'
END
WHERE claim_type IS NULL;

ALTER TABLE knowledge.claims ALTER COLUMN claim_type SET DEFAULT 'speculative';
ALTER TABLE knowledge.claims ALTER COLUMN claim_type SET NOT NULL;

-- Pin the enum at the DB so a bad write fails loudly rather than corrupting the
-- routing key (predicate/status are plain TEXT, but claim_type drives synthesis).
ALTER TABLE knowledge.claims DROP CONSTRAINT IF EXISTS claims_claim_type_check;
ALTER TABLE knowledge.claims ADD CONSTRAINT claims_claim_type_check
    CHECK (claim_type IN (
        'score', 'capability', 'comparison', 'attribution', 'lineage',
        'evaluation', 'reproduction', 'critique', 'speculative'
    ));

CREATE INDEX IF NOT EXISTS idx_claims_claim_type ON knowledge.claims (claim_type);
