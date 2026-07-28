-- Per-field numeric config overrides — the install target for config-kind
-- actuators (confidence weights, decay thresholds, …), the analog of
-- prompt_versions for numbers instead of prompts.
--
-- A config-A/B experiment that promotes writes the winning value here; the live
-- pipeline overlays these onto the env defaults per field. Keys are namespaced by
-- component (e.g. 'confidence.attack_weight', 'decay.halflife_days'). Append-only:
-- rows are kept, only is_active flips, so any value can be re-activated to roll back.
CREATE TABLE IF NOT EXISTS agents.field_config (
    id          TEXT PRIMARY KEY,
    field_id    TEXT NOT NULL REFERENCES catalog.fields(id),
    key         TEXT NOT NULL,
    value       DOUBLE PRECISION NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT false,
    rationale   TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- At most one active value per (field, key).
CREATE UNIQUE INDEX IF NOT EXISTS uq_field_config_active
    ON agents.field_config (field_id, key)
    WHERE is_active;

CREATE INDEX IF NOT EXISTS idx_field_config_field_key_time
    ON agents.field_config (field_id, key, created_at DESC);

GRANT SELECT, INSERT, UPDATE ON agents.field_config TO mesh_writer;
GRANT SELECT ON agents.field_config TO mesh_reader;
