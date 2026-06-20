# Scheduling

Phase 6a first wired the mesh up to run on its own cadence via APScheduler.
Phase 9 substantially reworked the control model — from a `BlockingScheduler`
driven by env-var crons into a non-blocking `BackgroundScheduler` driven by a
Postgres `schedules` table, fronted by an HTTP control surface and editable
live from the wiki. The scheduler now runs a single orchestration job per field:
the deterministic **controller** (`mesh-controller --apply`), which runs the whole
reactive loop. Manual triggers (`make controller` / `make controller-apply`) keep
working unchanged — the scheduler is purely additive.

## Current scheduler (Phase 9+)

### Architecture

- **`apps/scheduler/`** — a `mesh-scheduler` entry point that builds a
  non-blocking `BackgroundScheduler` (`SchedulerManager`) and serves a tiny
  Starlette HTTP control surface with uvicorn on the main thread, on **:9100**.
  Reuses `Dockerfile.coordinator` so it has the same workspace install and DB
  access as the manual run paths.
- **Config lives in Postgres, not env crons.** `SchedulerManager` reads
  interval (`interval_hours`) + `enabled` per job from a `schedules` table in
  the `mesh-postgres` container (`public` schema, accessed via
  `mesh_a2a.schedules`). `interval_hours` drives an APScheduler
  `IntervalTrigger`. Schedules are **field-scoped** (Phase 17): the table's
  primary key is `(job_id, field_id)`, so each field can run jobs on its own
  cadence. The default field (`ai-robotics`) keeps the bare `job_id` as its
  APScheduler id; other fields get a `job_id:field_id` suffix.
- **Job bodies subprocess to the existing CLI entry points.** No new dispatch
  logic, no in-process orchestration inside the scheduler. The scheduler is a
  trigger, not an orchestrator. The job shells out to the same command a human
  would run (`uv run mesh-controller --apply`), with `MESH_TRIGGERED_BY`,
  `MESH_RUN_ID`, and `MESH_PIPELINE_FIELD` injected. All DB writes happen on the
  controller side.
- **Live reconcile, no restart.** `SchedulerManager.reconcile()` re-reads the
  Postgres config and applies interval/enabled changes to the live jobs without
  a restart. It runs both on a **30s poll** (the safety net) and on an explicit
  `/scheduler/reload` signal from the API (so a UI change applies near-instantly).
  Reconcile only acts on actual transitions — it won't push next-fire-times
  forward on every poll.
- **Runtime state is in-memory.** `SchedulerManager` tracks per-job
  running / last-run-at / last-outcome / last-run-id under a lock (APScheduler
  fires jobs on a thread pool, manual runs spawn their own threads, and the
  HTTP server reads from the asyncio thread). Jobs are not persisted to a
  job store — the `pipeline_runs` table already serves as the audit log of
  what actually ran. A scheduled fire is skipped (not queued) if a run for that
  `(job, field)` is already in progress (`max_instances=1`, `coalesce=True`).

### Scheduled jobs

| Job id | Command | Default interval |
|---|---|---|
| `controller` | `mesh-controller --apply --field <field>` | every 6h |

The controller is the sole scheduled orchestration job; challenge, discovery,
belief consolidation, decay/archival, and memory consolidation are all controller
rules within it, not separate jobs. Defaults live in `DEFAULT_INTERVALS`
(`mesh_a2a.schedules`) and are seeded into the `schedules` table on first ensure
(`ON CONFLICT DO NOTHING`, so a populated table is left untouched). The job is
passed `--field`, run once per active field.

### HTTP control surface (:9100)

The scheduler exposes four routes (Starlette):

- `GET  /healthz` — liveness.
- `GET  /scheduler/status` — per-job `next_run_at` / `last_run_at` / state
  (`running` | `disabled` | `idle`), one row per `(job, field)`.
- `POST /scheduler/reload` — re-read Postgres config now (the API's reload
  signal); 503 if Postgres is unreachable.
- `POST /scheduler/run/{job_id}?field=<slug>` — start an immediate manual run;
  404 for an unknown job, 409 if a run for that `(job, field)` is already in
  progress.

### Editing the schedule (wiki + API)

Schedule config is editable from the wiki **Pipelines** page and the read API,
which proxies the scheduler over `SCHEDULER_URL` (degrading gracefully when the
scheduler is down):

- `GET  /api/v1/schedules` / `PATCH /api/v1/schedules` — read/write the Postgres
  `schedules` table (interval + enabled). A `PATCH` signals
  `/scheduler/reload` so the change applies live.
- `POST /api/v1/pipelines/{job_id}/trigger` — proxy to `/scheduler/run/{job_id}`.
- `GET  /api/v1/scheduler/status` — proxy to `/scheduler/status`.

## Running locally

```bash
docker compose --profile scheduler up scheduler -d
docker compose logs -f scheduler
```

The container starts the `BackgroundScheduler` (registering the `controller` job
per field from the Postgres `schedules` table, falling back to `DEFAULT_INTERVALS`
if the table is empty/unavailable) and serves the HTTP control surface on :9100.
The job fires on its interval and shells out to `uv run mesh-controller --apply`.
Those runs hit the same DB as your manual runs and tag their `pipeline_runs` row
with `triggered_by='scheduled'`.

To change cadence or enable/disable a job, edit it on the wiki **Pipelines**
page (or `PATCH /api/v1/schedules`) — no restart needed, the change reconciles
to the live job within 30s (or instantly via the reload signal).

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `SCHEDULER_URL` | `http://scheduler:9100` | API → scheduler control endpoint (status / trigger / reload). |
| `SCHEDULER_HOST` / `SCHEDULER_PORT` | `0.0.0.0` / `9100` | Scheduler HTTP control bind host/port (`MESH_BIND_INTERFACE` wins if set). |
| `LANGGRAPH_POSTGRES_URL` | (unset) | DSN for `mesh-postgres`, which holds the `schedules` table. Unset → schedule endpoints 503 and the scheduler falls back to `DEFAULT_INTERVALS`. |
| `MESH_TRIGGERED_BY` | (unset → `manual`) | Set by the scheduler to `scheduled` per run. Manual `make controller-apply` runs leave it unset. |
| `MESH_CURATOR_STALENESS_WEIGHT` | `0.3` | How heavily the Curator weights "no fresh supporting/contradicting claim in a while" when picking beliefs for the Skeptic. |

> The legacy env-var crons (`MESH_SCHEDULE_PIPELINE_CRON`,
> `MESH_SCHEDULE_SWEEP_CRON`) are no longer the live schedule — interval/enabled
> comes from Postgres. They survive only behind `configured_cron_triggers()`,
> which feeds the legacy `/status` HTML page Phase 9 intentionally left as-is.

## Observing

`GET /api/v1/scheduler/status` (or the wiki **Pipelines** page) shows per-job
next/last run and state. The scheduler's own `GET /scheduler/status` returns the
same data directly. `pipeline_runs` remains the durable audit log of what
actually ran (with `triggered_by`).

## Staleness signal

Phase 6a also added two related pieces (still in effect):

- `mesh_db.beliefs.find_stale_beliefs(threshold_days)` — query helper for
  "beliefs whose most recent supporting/contradicting claim is older than N
  days, or who have no claims attached at all."
- `BeliefForCuration.last_evidence_at` + a factor in `score_belief()` — the
  Skeptic sweep populates the evidence timestamp per belief, and the Curator
  multiplies it by `MESH_CURATOR_STALENESS_WEIGHT` (default 0.3) when ranking.
  No claims → max staleness.

This is additive on top of the existing `age` signal (which tracks
`last_revised_at` on the belief itself). The two are different: a belief can be
revised recently by the Skeptic while its underlying supporting claims grow
stale, and the new signal surfaces those.

## What's out of scope

- Missed-run replay — APScheduler defaults (`coalesce=True`,
  `max_instances=1`) mean a downtime gap yields at most one catch-up run, not a
  backlog. A scheduled fire that overlaps a running job is skipped, not queued.
- Distributed / multi-process scheduling.
- Per-agent or per-source schedules — only the top-level controller runs on a
  timer (per field); its rules (challenge, discovery, consolidation,
  decay/archival, memory) fire from within a run.
- Persistent job store — runtime state is in-memory; `pipeline_runs` is the
  durable audit log.
