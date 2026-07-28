-- Installed system-prompt versions per (field, skill) — the sink of the
-- autonomous prompt-improvement loop. A candidate that wins a held-out A/B
-- against the live prompt is inserted here as the new active version; the live
-- extractor reads the active row and falls back to the built-in prompt when
-- none exists.
--
-- Append-only content: rows are never updated (except the is_active status flag)
-- and never deleted, so the full prompt lineage — and the A/B evidence that
-- promoted each one — stays auditable and any version can be re-activated to roll
-- back. A partial unique index enforces at most one active version per
-- (field, skill); installing flips the prior active off and inserts the new one
-- in a single transaction.
CREATE TABLE IF NOT EXISTS catalog.prompt_versions (
    id                    TEXT PRIMARY KEY,
    field_id              TEXT NOT NULL REFERENCES catalog.fields(id),
    skill_key             TEXT NOT NULL,
    prompt                TEXT NOT NULL,
    is_active             BOOLEAN NOT NULL DEFAULT false,
    dataset_field         TEXT,
    baseline_f1           DOUBLE PRECISION,
    best_f1               DOUBLE PRECISION,
    holdout_baseline_f1   DOUBLE PRECISION,
    holdout_best_f1       DOUBLE PRECISION,
    holdout_gain          DOUBLE PRECISION,
    rationale             TEXT,
    proposer_tokens       INTEGER NOT NULL DEFAULT 0,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- At most one active version per (field, skill).
CREATE UNIQUE INDEX IF NOT EXISTS uq_prompt_versions_active
    ON catalog.prompt_versions (field_id, skill_key)
    WHERE is_active;

-- Active-lookup on the hot path + lineage listing (newest first).
CREATE INDEX IF NOT EXISTS idx_prompt_versions_field_skill_time
    ON catalog.prompt_versions (field_id, skill_key, created_at DESC);

GRANT SELECT, INSERT, UPDATE ON catalog.prompt_versions TO mesh_writer;
GRANT SELECT ON catalog.prompt_versions TO mesh_reader;
