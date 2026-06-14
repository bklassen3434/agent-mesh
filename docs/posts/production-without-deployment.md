# Production-ready without deployment

> Draft. Phase 7d.

The default version of this story is "we built a thing, then we deployed
it to the cloud, then it was production." The Agent Mesh story is the
inverse: the cloud step never happened, but the thing crossed every other
production threshold along the way. This is a write-up of the three moves
that did the work: scheduling on top of an async wire protocol, persisting
dispatch lifecycle so crashes are visible, and treating Tailscale as the
auth layer.

## The framing problem

Production-readiness gets conflated with two things it isn't.

It isn't the URL. A side project that runs on `localhost` can be
production-ready; a hosted service can be a fragile demo. The URL is
about distribution, not readiness.

It isn't multi-tenancy. Building a Users table on day one for a tool
you're going to be the only user of is a tax — you pay for permission
models, session handling, and access-control branches that never earn
their cost. Single-user is the right shape when the actual user count is
one.

What production-readiness actually is, the property that decides whether
you trust the thing to run while you're not looking:

1. It runs on a schedule without you typing anything.
2. When it crashes, you can see what it was doing.
3. You can reach it from your phone when you're away from the laptop.

Agent Mesh got there. Here's how.

## Move 1: async A2A so durability has somewhere to live

Phase 5a was the substrate shift. The A2A protocol moved from
`message/send` (sync, blocking) to a task-based `submit + poll` pattern:

```python
async with MeshA2AClient(task_recorder=recorder) as client:
    result = await client.call_skill_blocking(
        "extract_claims", payload, traceparent=tp
    )
```

`call_skill_blocking` keeps the *appearance* of a synchronous call — the
caller awaits a result — but underneath, the wire protocol is fully
task-based. Submit returns a `task_id`. The client polls
`GET /mesh/tasks/{task_id}` until status is `completed` or `failed`. The
agent's `TaskRegistry` is in-memory; if the agent crashes mid-task, the
poll returns 404 and the orchestrator handles it gracefully.

This was the prerequisite for everything else in Phase 6. You can't bolt
"durable dispatch" onto a sync protocol — there's no observable lifecycle
to persist, no place for a heartbeat. The async pattern creates the
hooks; persistence fills them.

## Move 2: scheduling on top of the async protocol

Phase 6a added APScheduler. Two cron jobs — pipeline every 6h, sweep
daily at 03:00 — running in their own container, profile-gated so they
stay out of `make up` by default.

The crucial detail is what the scheduler doesn't do. It is not an
orchestrator. It shells out to the existing CLI entry points
(`mesh-ingest`, `mesh-skeptic`) the same way `make ingest` does
when a human runs it manually. The only extra information the scheduler
threads in is a `MESH_TRIGGERED_BY=scheduled` env var that lands on the
`pipeline_runs.triggered_by` column.

This means: manual runs and scheduled runs go through identical code
paths. There's no second pipeline for scheduled execution. The audit log
is shared. A manual `make ingest` while the scheduler is also running
doesn't collide — the `pipeline_runs` row insertion is the lock.

```
$ uv run mesh.cli schedule status
                                 Mesh schedule
┌──────────────┬─────────────────────┬───────────────────┬──────────┬──────────────┬──────────────────────────┐
│ Job          │ Next run            │ Last run          │ Duration │ Triggered by │ Counts                   │
├──────────────┼─────────────────────┼───────────────────┼──────────┼──────────────┼──────────────────────────┤
│ ingest       │ 2026-05-28 06:00 …  │ 2026-05-28 00:00  │ 142s     │ scheduled    │ claims +18 / beliefs …  │
│ skeptic      │ 2026-05-28 03:00 …  │ 2026-05-27 03:00  │ 38s      │ scheduled    │ beliefs ~3              │
└──────────────┴─────────────────────┴───────────────────┴──────────┴──────────────┴──────────────────────────┘
```

That's "I haven't typed anything in 24 hours, and the mesh has been
working" surfaced in one CLI call. The first production threshold.

## Move 3: orchestrator-side task durability

Phase 6b is where the dispatch lifecycle gets persisted. Two new DuckDB
tables, `agent_tasks` (rows) and `agent_task_events` (append-only audit
log), with a typed Python DAL and a `DuckDBTaskRecorder` that
`MeshA2AClient` calls into via a `TaskRecorder` Protocol.

Every dispatch now writes:

- `created` event when `submit_task` returns
- `started` when the first poll sees a `running` status
- `heartbeat` every N polls (default 20 → roughly every 10s at the 0.5s
  poll interval)
- `completed` / `failed` on terminal state

The agent-side state remains in-memory and ephemeral. The point isn't to
resume a dead task — that would require persisting in-flight task state
inside each agent, which is genuinely hard to get right. The point is
observability: when something crashes, you can see what was in flight.

The recovery story is "fail visibly, don't try to resume." On startup,
the coordinator and skeptic-sweep call
`sweep_orphaned_tasks(threshold_seconds=600)` which marks any
pending/running tasks whose `updated_at` is older than 10 minutes as
`failed` with `error='orphaned_on_restart'`. No retry, no resumption.
The operator can re-run the pipeline manually and the
`/status` page surfaces the orphans in the "recent failures" panel.

Why this is enough: in the local-first single-user model, retries are a
trap. `extract_claims` isn't idempotent — running it twice on the same
source produces duplicate claims unless dedup is bulletproof, which it
isn't yet. Failing visibly preserves the operator's option to look at
what happened and decide. Auto-retry without idempotency would corrupt
data in pursuit of resilience.

## Move 4: Tailscale as auth

The third production threshold — reach it from your phone — is where the
"deploy publicly" story usually shows up. Public DNS, TLS, Cloudflare,
auth layer, login flow.

Phase 6b skipped all of it. The API + wiki bind to a `MESH_BIND_INTERFACE`
env var (default `0.0.0.0` for dev) which is set to the host's tailnet IP
(`100.x.x.x`) for production. Run the containers in host network mode.
Done.

The mesh is reachable from any device on your tailnet. Not reachable from
any device that isn't. Tailscale handles the TLS, the identity, the
network ACL, the firewall — all of it. The mesh sees plain HTTP on its
side because the WireGuard tunnel encrypts everything end-to-end already.

This isn't novel. Tailscale's "the network is the auth" pitch is the
whole product. But it's worth saying out loud that a single-user portfolio
project doesn't need to write a single line of auth code to be reachable
from the world. The auth lives at the network layer, where it always has.

## The status page proves it

The `/status` route at `<api>/status` is the operational surface that
ties everything together. Server-rendered HTML, no JS, meta-refresh every
60s. Four panels: last + next runs (with `triggered_by`, deltas), row
counts, recent task failures from `agent_tasks`, Langfuse 24h trace count
when configured.

This is the page you look at on your phone while waiting for coffee. It
either reads "the mesh is healthy" at a glance, or it reads "the mesh
has been failing for 6 hours, here's the last error." That distinction
is the production threshold.

## What this isn't

It's not a story about scale. Single user, single laptop, ~hundreds of
claims per day at the absolute upper bound. The DB is DuckDB on local
disk; the agents are docker-compose containers; the cache is in-process.
None of that scales horizontally and none of it needs to.

It's not a story about novelty. Async dispatch with durability is the
default for any serious queue system. Tailscale-as-auth is an established
pattern. APScheduler is a well-trodden library.

It's a story about what the threshold actually is. Production-readiness
turned out to be three properties — scheduled, durable, reachable —
and those properties can be built on a laptop. The decision to deploy
publicly becomes a separate, optional follow-up, instead of being
load-bearing on the question "is this thing real yet."

The answer was yes, before the URL existed.
