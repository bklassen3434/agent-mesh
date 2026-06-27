-- Beta chatbot quota ledger (user-control phase).
--
-- Anonymous "beta" visitors of the wiki get a capped number of chatbot
-- questions per day. The wiki issues each browser a stable beta id (cookie) and
-- proxies every ask through its own server; the API counts consumed questions
-- here, one row per (beta_id, day). Authenticated admins bypass the quota and
-- never appear in this table.
--
-- Operational state, not knowledge: the API's quota check writes these rows
-- directly (writer role), the same way schedules/connectors are written from the
-- wiki — they never flow through the effect gateway. Keeping the count
-- server-side is the whole point: a browser can clear its cookie to look like a
-- new visitor, but it can't reset a count it never sees.
CREATE TABLE IF NOT EXISTS runtime.beta_query_log (
    beta_id TEXT NOT NULL,
    day     DATE NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (beta_id, day)
);

CREATE INDEX IF NOT EXISTS idx_beta_query_log_day
    ON runtime.beta_query_log (day);

-- Explicit grants (belt-and-suspenders alongside the runtime schema default
-- privileges set in migration 015): the API's quota check both reads and writes.
GRANT SELECT, INSERT, UPDATE ON runtime.beta_query_log TO mesh_writer;
GRANT SELECT ON runtime.beta_query_log TO mesh_reader;
