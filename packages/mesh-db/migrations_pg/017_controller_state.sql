-- Deterministic controller: per-tension dispatch state.
--
-- The rule-based controller (the auction-free replacement for the market) is a
-- pure function of (board state, stored counters, now). This table holds the
-- stored counters: one row per (field, tension) recording how many times the
-- controller has dispatched that tension and what came of the last attempt. It
-- is what makes cooldowns, oscillation control, and swarm-escalation
-- deterministic across invocations — no daemon, no wall-clock watcher.
--
--   * attempts          — how many times this tension has been dispatched (ever).
--   * last_outcome      — 'effects' | 'no_effects' | 'error' from the last run.
--   * last_effect_count — how many effects the last dispatch produced.
--   * last_attempt_at   — when (so a rule can express a cooldown against `now`).
--
-- Operational state, not knowledge: the controller owns these writes directly
-- (writer role), the same way it owns the pipeline_runs ledger — they never flow
-- through the effect gateway. Tension ids are stable ("<kind>:<target>"), so a
-- row's identity survives recomputation of the board.
CREATE TABLE IF NOT EXISTS runtime.controller_tension_state (
    field_id          TEXT NOT NULL,
    tension_id        TEXT NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_outcome      TEXT,
    last_effect_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (field_id, tension_id)
);

CREATE INDEX IF NOT EXISTS idx_controller_state_field
    ON runtime.controller_tension_state (field_id);

-- Explicit grants (belt-and-suspenders alongside the schema default privileges
-- migration 015 set on `runtime`): the controller writes, the API only reads.
GRANT SELECT, INSERT, UPDATE ON runtime.controller_tension_state TO mesh_writer;
GRANT SELECT ON runtime.controller_tension_state TO mesh_reader;
