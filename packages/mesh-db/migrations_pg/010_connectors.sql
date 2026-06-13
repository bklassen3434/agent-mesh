-- Phase 17c: the source-connector catalog + per-field enablement.
--
-- knowledge.connectors is the GLOBAL catalog of connector definitions (reusable
-- across fields): a slug, display name, description, kind (builtin this phase),
-- and a config_schema describing the fields a field must supply. It is seeded
-- from the Python registry (mesh_models.connector.BUILTIN_CONNECTORS) by init_pg
-- so the config_schema JSON lives in one place — the SQL runner only creates the
-- empty tables and grants.
--
-- knowledge.field_connectors is one field's enablement + config of a catalog
-- connector (coordinator-write). The coordinator dispatches only the connectors
-- enabled for a run's field, passing each its stored config. The ai-robotics
-- field's rows (every built-in enabled, config = today's scout defaults) are
-- seeded by init_pg too, so the seeded field behaves exactly as before.

CREATE TABLE knowledge.connectors (
    id            TEXT PRIMARY KEY,
    slug          TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'builtin',
    config_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE knowledge.field_connectors (
    field_id     TEXT NOT NULL REFERENCES knowledge.fields(id),
    connector_id TEXT NOT NULL REFERENCES knowledge.connectors(id),
    config       JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (field_id, connector_id)
);

CREATE INDEX idx_field_connectors_field ON knowledge.field_connectors (field_id);

-- Grants: catalog is reader-readable + writer-writable (no DELETE — built-ins
-- persist). Per-field enablement is coordinator-writer-owned, reader-readable.
GRANT SELECT, INSERT, UPDATE ON knowledge.connectors TO mesh_writer;
GRANT SELECT ON knowledge.connectors TO mesh_reader;
GRANT SELECT, INSERT, UPDATE ON knowledge.field_connectors TO mesh_writer;
GRANT SELECT ON knowledge.field_connectors TO mesh_reader;
