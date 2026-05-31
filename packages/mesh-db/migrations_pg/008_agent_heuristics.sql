-- Phase 16b: the procedural memory store.
--
-- Agents accumulate revisable, provenance-grounded *heuristics* (learned
-- how-to), distilled offline from their episodic history (Phase 15) by the
-- consolidation job (Phase 16c). Modeled on the belief / belief_revision pair:
-- a mutable head row (agent_heuristic) plus an append-only revision log
-- (agent_heuristic_revision). Same invariants:
--
--   * Coordinator-owned writes. Only mesh_writer writes these tables (granted
--     below); no agent role gains write. Agents propose over A2A; the
--     coordinator persists.
--   * Revised append-only. Every change to a heuristic writes a revision row;
--     revisions are never updated or deleted (no DELETE granted — mirrors the
--     belief_revisions posture).
--   * Provenance mandatory. Every heuristic (and revision) links to the runs +
--     claims that justify it (provenance_run_ids / provenance_claim_ids), the
--     same array-valued provenance pattern beliefs use for claim ids.
--
-- TTL: every heuristic carries an expires_at; consumption (Phase 16d) excludes
-- expired rows. is_currently_active mirrors beliefs.is_currently_held so a
-- heuristic can be retired without deletion.

CREATE TABLE knowledge.agent_heuristic (
    id                   TEXT PRIMARY KEY,
    -- scope: which agent + skill this heuristic applies to (matches the agent
    -- identity stamped on runs/artifacts and the derived skill id).
    agent                TEXT NOT NULL,
    skill                TEXT NOT NULL,
    -- optional finer scope: a source type/url or a specific entity.
    source               TEXT,
    entity_id            TEXT REFERENCES knowledge.entities(id),
    heuristic            TEXT NOT NULL,
    -- start low; earns trust over time (Phase 16b default; model mirrors this).
    confidence           DOUBLE PRECISION NOT NULL DEFAULT 0.3,
    -- mandatory provenance: the runs + claims that justify this heuristic.
    provenance_run_ids   TEXT[] DEFAULT '{}',
    provenance_claim_ids TEXT[] DEFAULT '{}',
    created_at           TIMESTAMPTZ NOT NULL,
    last_revised_at      TIMESTAMPTZ NOT NULL,
    revision_count       INTEGER NOT NULL DEFAULT 0,
    -- TTL: consumption excludes rows past expires_at.
    expires_at           TIMESTAMPTZ NOT NULL,
    is_currently_active  BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE knowledge.agent_heuristic_revision (
    id                   TEXT PRIMARY KEY,
    heuristic_id         TEXT NOT NULL REFERENCES knowledge.agent_heuristic(id),
    previous_heuristic   TEXT NOT NULL,
    new_heuristic        TEXT NOT NULL,
    previous_confidence  DOUBLE PRECISION NOT NULL,
    new_confidence       DOUBLE PRECISION NOT NULL,
    provenance_run_ids   TEXT[] DEFAULT '{}',
    provenance_claim_ids TEXT[] DEFAULT '{}',
    revised_by_agent     TEXT NOT NULL,
    revised_at           TIMESTAMPTZ NOT NULL,
    rationale            TEXT NOT NULL
);

-- Scope-matched, unexpired retrieval is the hot read path (Phase 16d):
-- filter by (agent, skill, is_currently_active) then expires_at.
CREATE INDEX idx_agent_heuristic_scope
    ON knowledge.agent_heuristic (agent, skill, is_currently_active);
CREATE INDEX idx_agent_heuristic_expires_at
    ON knowledge.agent_heuristic (expires_at);
CREATE INDEX idx_agent_heuristic_revision_heuristic_id
    ON knowledge.agent_heuristic_revision (heuristic_id);

-- Write-ownership: coordinator-writer gets SELECT/INSERT/UPDATE (NOT DELETE, so
-- the append-only / no-silent-overwrite invariant holds at the DB level, like
-- claims + belief_revisions). api-readonly gets SELECT only. No agent role is
-- granted any write here. (Migration 005's ALTER DEFAULT PRIVILEGES would also
-- cover these; granting explicitly keeps the intent legible alongside 006/007.)
GRANT SELECT, INSERT, UPDATE ON knowledge.agent_heuristic TO mesh_writer;
GRANT SELECT, INSERT, UPDATE ON knowledge.agent_heuristic_revision TO mesh_writer;
GRANT SELECT ON knowledge.agent_heuristic TO mesh_reader;
GRANT SELECT ON knowledge.agent_heuristic_revision TO mesh_reader;
