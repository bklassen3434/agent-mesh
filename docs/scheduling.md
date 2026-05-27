# Scheduling

Phase 6a wired the mesh up to run on its own cadence via APScheduler.
Two jobs are registered: the ingestion pipeline and the skeptic
falsification sweep. Both manual triggers (`make pipeline`,
`make skeptic`) keep working unchanged — the scheduler is purely
additive.

## Architecture

- **`apps/scheduler/`** — a `mesh-scheduler` CLI that builds a
  `BlockingScheduler`, registers two jobs against env-configured cron
  expressions, and blocks forever. Reuses `Dockerfile.coordinator` so
  it has the same workspace install and DB volume as the manual run
  paths.
- **Job bodies subprocess to the existing CLI entry points.** No new
  dispatch logic, no in-process orchestration inside the scheduler.
  The scheduler is a trigger, not an orchestrator.
- **In-memory job store, single thread pool.** APScheduler defaults.
  We do not persist jobs to disk (no `SQLAlchemyJobStore`) — the
  `pipeline_runs` table already serves as the audit log of what
  actually ran.
- **Missed runs are not replayed.** APScheduler defaults to
  `coalesce=True` and the default `misfire_grace_time`, which means
  a 6-hour gap due to a downed scheduler results in zero or one
  catch-up run, not a backlog. This is intentional for Phase 6a.

## Running locally

```bash
docker compose --profile scheduler up scheduler -d
docker compose logs -f scheduler
```

The container will sit idle until the next cron fire-time, at which
point it shells out to `uv run mesh-pipeline --a2a` (or
`mesh-skeptic-sweep`). Those runs hit the same DB as your manual
runs and tag their `pipeline_runs` row with `triggered_by='scheduled'`.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `MESH_SCHEDULE_PIPELINE_CRON` | `0 */6 * * *` | Every 6 hours, on the hour. |
| `MESH_SCHEDULE_SWEEP_CRON` | `0 3 * * *` | Daily at 03:00 — offset from the pipeline cadence so the sweep doesn't collide. |
| `MESH_TRIGGERED_BY` | (unset → `manual`) | Set by the scheduler to `scheduled`. Manual `make pipeline` runs leave it unset. |
| `MESH_CURATOR_STALENESS_WEIGHT` | `0.3` | New: how heavily the Curator weights "no fresh supporting/contradicting claim in a while" when picking beliefs for the Skeptic. |

For faster verification (e.g. the 90-minute window in the phase exit
criteria), override the crons inline:

```bash
MESH_SCHEDULE_PIPELINE_CRON='*/20 * * * *' \
MESH_SCHEDULE_SWEEP_CRON='*/30 * * * *' \
docker compose --profile scheduler up scheduler
```

## Observing

```bash
uv run mesh.cli schedule status
```

Shows next-fire-time for each job and the latest row from
`pipeline_runs` of the matching type. Sample output:

```
Mesh schedule
┌──────────────┬─────────────────────┬───────────────────┬──────────┬──────────────┬──────────────────────────┐
│ Job          │ Next run            │ Last run          │ Duration │ Triggered by │ Counts                   │
├──────────────┼─────────────────────┼───────────────────┼──────────┼──────────────┼──────────────────────────┤
│ pipeline     │ 2026-05-27 06:00 …  │ 2026-05-27 00:00  │ 142s     │ scheduled    │ claims +18 / beliefs …  │
│ skeptic_sweep│ 2026-05-27 03:00 …  │ 2026-05-25 17:51  │ 0s       │ manual       │ beliefs ~0              │
└──────────────┴─────────────────────┴───────────────────┴──────────┴──────────────┴──────────────────────────┘
```

## Staleness signal

Phase 6a also added two related pieces:

- `mesh_db.beliefs.find_stale_beliefs(threshold_days)` — query helper
  for "beliefs whose most recent supporting/contradicting claim is
  older than N days, or who have no claims attached at all."
- `BeliefForCuration.last_evidence_at` + a new factor in
  `score_belief()` — the Skeptic sweep populates the evidence
  timestamp per belief, and the Curator multiplies it by
  `MESH_CURATOR_STALENESS_WEIGHT` (default 0.3) when ranking. No
  claims → max staleness.

This is additive on top of the existing `age` signal (which tracks
`last_revised_at` on the belief itself). The two are different:
a belief can be revised recently by the Skeptic while its underlying
supporting claims grow stale, and the new signal surfaces those.

## What's out of scope (Phase 6a)

- Missed-run replay.
- Distributed / multi-process scheduling.
- Per-agent or per-source schedules — only the two top-level
  orchestrators run on cron.
- A web UI for editing the schedule.
- Persistent job store. (See `pipeline_runs` for the audit log.)
