-- Phase 22a: investigation origin + provenance.
--
-- Investigations can now be opened by more than the Curator: the reactive
-- per-belief Curator path (origin='curator'), the skeptic sweep (origin=
-- 'skeptic'), the proactive whole-field discovery sweep (origin='discovery'),
-- and humans (origin='manual'). `origin` makes an autonomous investigation
-- distinguishable; `trigger_rationale` records the human-readable "why we
-- opened this" (the gap/trend signals that fired) so self-direction is
-- explainable and auditable.
--
-- Backfill: every existing row predates discovery, so it was Curator-opened.
-- Grants unchanged (writer insert/update, reader select; no DELETE).

ALTER TABLE knowledge.investigations
    ADD COLUMN origin TEXT NOT NULL DEFAULT 'curator';
ALTER TABLE knowledge.investigations
    ADD COLUMN trigger_rationale TEXT;

UPDATE knowledge.investigations SET origin = 'curator';

CREATE INDEX idx_investigations_field_origin
    ON knowledge.investigations (field_id, origin);
