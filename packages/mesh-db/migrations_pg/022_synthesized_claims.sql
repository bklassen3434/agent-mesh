-- Terminal state for the unsynthesized_claims tension.
--
-- The per-entity synthesize tension fired whenever an entity had a synthesizable
-- claim not *physically present* in a held belief's arrays or a relationship's
-- evidence. But synthesis legitimately produces no membership for many claims:
-- a score that isn't the leaderboard record-holder (SOTA keeps only the leader),
-- a capability the belief already covers, a relational claim already edged. Those
-- claims stayed "unsynthesized" forever, so their entity re-fired the tension
-- every pass — a no-op churn (114k dispatches/day) that also kept the board from
-- ever being idle, which is the precondition for scouting (so ingestion stalled).
--
-- synthesized_claims records that synthesize-belief has *processed* a claim
-- (whatever the outcome). The count excludes marked claims, so a fully-processed
-- entity drops out of the trigger until a genuinely NEW claim arrives (the new
-- claim is unmarked → re-fires the tension → synthesize re-reads the entity's full
-- claim set → SOTA/capability recompute → the new claim is marked). Append-only;
-- FK-cascades so a superseded/merged claim's marker disappears with it.
CREATE TABLE IF NOT EXISTS knowledge.synthesized_claims (
    claim_id       TEXT PRIMARY KEY REFERENCES knowledge.claims(id) ON DELETE CASCADE,
    synthesized_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
