-- Phase 6b orchestrator-side task durability.
-- The agent-side TaskRegistry stays in-memory, these tables let us answer
-- "what was the mesh doing when it crashed" after the orchestrator restarts.
-- Agent crashes manifest as running tasks that the startup sweep orphans.

CREATE TABLE IF NOT EXISTS agent_tasks (
    id VARCHAR PRIMARY KEY,
    skill_id VARCHAR NOT NULL,
    agent_url VARCHAR NOT NULL,
    status VARCHAR NOT NULL,  -- pending | running | completed | failed
    input JSON NOT NULL,
    output JSON,
    error VARCHAR,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    dispatched_by_run_id VARCHAR
);

-- Append-only audit log. Never UPDATE — insert a fresh row per event.
CREATE TABLE IF NOT EXISTS agent_task_events (
    id VARCHAR PRIMARY KEY,
    task_id VARCHAR NOT NULL,
    event_type VARCHAR NOT NULL,  -- created | started | heartbeat | completed | failed
    timestamp TIMESTAMPTZ NOT NULL,
    detail JSON
);

CREATE INDEX IF NOT EXISTS idx_agent_tasks_status ON agent_tasks(status);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_run_id ON agent_tasks(dispatched_by_run_id);
CREATE INDEX IF NOT EXISTS idx_agent_task_events_task_id ON agent_task_events(task_id);
