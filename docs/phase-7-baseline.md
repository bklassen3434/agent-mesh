# Phase 7 baseline

Snapshot at the start of Phase 7 work, after the pre-work commits
(failure_mode enum + reproduction exploration). Used to verify that
7a's effect is "investigations land in the new table + lifecycle
runs cleanly," not structural drift elsewhere.

## Schema state (post pre-work)

- `claims.failure_mode` column exists (migration 013). Skeptic-authored
  rows backfilled to `'other'`; all others `NULL`.
- `agent_tasks` / `agent_task_events` from 6b unchanged.
- `pipeline_runs.triggered_by` from 6a unchanged.

## Row counts (local dev DB)

| Entity | Count |
|---|---|
| `pipeline_runs` (`run_type='ingest'`) | 0 |
| `pipeline_runs` (`run_type='skeptic'`) | 1 |
| `claims` | 0 |
| `beliefs` | 0 |
| `belief_revisions` | 0 |
| `sources` | 0 |
| `entities` | 0 |
| `investigations` | 0 |
| `agent_tasks` | 0 |

Same essentially-empty state as the 6a baseline. Phase 7a verification
will focus on schema correctness + lifecycle transitions rather than
row-count deltas.

## What 7a adds

- Reshaped `Investigation` model with structured fields
  (`target_entity_id`, `hypothesis`, `suggested_source_types`,
  `status`, `opened_by_belief_id`).
- Migration to update the `investigations` table.
- `mesh.cli investigations list` command.
- Curator emits investigation suggestions alongside belief picks.
- Each scout adds an `investigate` skill.
- Coordinator queries open investigations on each pipeline run and
  dispatches them after the standard scout phase.

Post-7a, the row-count expectation is: `investigations` ≥ 1 after a
sweep that finds a stale belief; `claims` may grow when investigations
return new evidence.
