-- Durable "these two entities are NOT the same" verdicts from the
-- merge-candidate adjudicator. Without this, a no-merge verdict was stored
-- nowhere: the duplicate-pair scan re-derived the same pair every sensing
-- pass, the same LLM question was re-asked every round (x3 under swarm
-- escalation), and one ambiguous pair could burn thousands of calls a day.
--
-- The pair is stored ordered (id_a < id_b), matching the scan's e2.id > e1.id
-- join. FKs cascade so a rejection disappears with either entity (a later
-- merge deletes the duplicate row via merge_entities).
CREATE TABLE IF NOT EXISTS knowledge.entity_merge_rejections (
    entity_id_a TEXT NOT NULL REFERENCES knowledge.entities(id) ON DELETE CASCADE,
    entity_id_b TEXT NOT NULL REFERENCES knowledge.entities(id) ON DELETE CASCADE,
    field_id    TEXT NOT NULL REFERENCES catalog.fields(id),
    similarity  DOUBLE PRECISION,
    rejected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_id_a, entity_id_b),
    CHECK (entity_id_a < entity_id_b)
);

GRANT SELECT, INSERT, DELETE ON knowledge.entity_merge_rejections TO mesh_writer;
GRANT SELECT ON knowledge.entity_merge_rejections TO mesh_reader;
