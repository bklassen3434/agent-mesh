-- Accumulated fault-attributions — the self-improvement loop's evidence store.
--
-- An accuracy/freshness eval writes one row per fault it attributes; it edits
-- nothing else. Rows accumulate over many eval runs so a noisy per-run signal
-- becomes durable, countable evidence. A derived tension counts the OPEN rows per
-- (field, component, target) and fires an A/B improve pass only once they cross a
-- threshold — the same accumulate-then-activate pattern as the rest of the board.
--
-- Append-only content: rows are never updated (except the status flip
-- open→resolved/dismissed at the end of an improve pass) and never deleted, so the
-- full history of what the system got wrong — and what fixed it — stays auditable.
CREATE TABLE IF NOT EXISTS agents.improvement_concerns (
    id            TEXT PRIMARY KEY,
    field_id      TEXT NOT NULL REFERENCES catalog.fields(id),
    component     TEXT NOT NULL,
    target        TEXT NOT NULL DEFAULT '',
    belief_id     TEXT NOT NULL DEFAULT '',
    verdict       TEXT NOT NULL DEFAULT '',
    severity      DOUBLE PRECISION NOT NULL DEFAULT 0,
    summary       TEXT NOT NULL DEFAULT '',
    evidence_urls TEXT[] NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'open',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at   TIMESTAMPTZ,
    resolved_by   TEXT NOT NULL DEFAULT ''
);

-- The activation query: count/scan OPEN concerns per (field, component, target).
CREATE INDEX IF NOT EXISTS idx_improvement_concerns_open
    ON agents.improvement_concerns (field_id, component, target, status);

-- Don't file the same belief's fault against the same component twice (an eval
-- re-run over the same still-open belief is not new evidence). A partial unique
-- index over the OPEN rows keeps the count honest without blocking a re-file after
-- the prior one was resolved.
CREATE UNIQUE INDEX IF NOT EXISTS uq_improvement_concerns_open_dedup
    ON agents.improvement_concerns (field_id, component, target, belief_id)
    WHERE status = 'open';

GRANT SELECT, INSERT, UPDATE ON agents.improvement_concerns TO mesh_writer;
GRANT SELECT ON agents.improvement_concerns TO mesh_reader;
