-- LLM-written "state of the field" narratives (the Field Overview page's
-- headline). Append-only: the write-field-brief skill inserts one row per
-- generation (cooldown-gated by the controller); readers take the latest per
-- field. inputs_summary snapshots the counts the narrative was written from,
-- so a brief is auditable against the store it described.
CREATE TABLE IF NOT EXISTS knowledge.field_briefs (
    id              TEXT PRIMARY KEY,
    field_id        TEXT NOT NULL REFERENCES catalog.fields(id),
    narrative       TEXT NOT NULL,
    model           TEXT,
    inputs_summary  JSONB NOT NULL DEFAULT '{}'::jsonb,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_field_briefs_field_time
    ON knowledge.field_briefs (field_id, generated_at DESC);

GRANT SELECT, INSERT ON knowledge.field_briefs TO mesh_writer;
GRANT SELECT ON knowledge.field_briefs TO mesh_reader;
