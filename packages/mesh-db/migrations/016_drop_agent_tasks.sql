-- Phase 8: LangGraph checkpointing replaces orchestrator-side task durability.
-- The agent_tasks + agent_task_events tables (Phase 6b) are no longer written
-- (the coordinator + skeptic-sweep graphs checkpoint to Postgres) and the
-- /status page now reads run state from the checkpoint store. Drop them.
-- Indexes are dropped implicitly with their tables.

DROP TABLE IF EXISTS agent_task_events;
DROP TABLE IF EXISTS agent_tasks;
