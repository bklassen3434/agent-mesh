# Deployment

Phase 6b's deployment story is **local-first with Tailscale access**:
the mesh runs on a single laptop, the wiki / API / status page are
reachable from your phone via a `*.ts.net` hostname, and no public DNS
record ever points at your machine.

This document covers two flows:

1. Local development with `make up` — everything on localhost, no
   Tailscale required.
2. Tailscale-only access — same `make up`, but services bind to the
   tailnet interface and not localhost.

## Prerequisites

- A Tailscale account. Free tier covers everything here (3 users, 100
  devices). Sign up at tailscale.com.
- The Tailscale daemon installed on the laptop, logged in, and
  enrolled in your tailnet. Verify:

  ```bash
  tailscale status
  ```

  …should list this machine.

- Your laptop's tailnet IP (a `100.x.x.x` address). Grab it with:

  ```bash
  tailscale ip -4
  ```

  Call this `$TAILNET_IP` below.

## Local development (no Tailscale)

Default behavior, unchanged from earlier phases:

```bash
make up
```

API at `http://localhost:8000`, wiki at `http://localhost:3000`,
status at `http://localhost:8000/status`.

`MESH_BIND_INTERFACE` should be empty (the default in
`.env.example`). The API binds to `0.0.0.0`, the wiki binds Next.js
to `0.0.0.0` via `HOSTNAME`.

## Tailscale-only access

The goal: wiki + API + status page reachable from any tailnet device,
*not* reachable from non-tailnet devices on the same wifi.

### Step 1 — set the bind address

In `.env`:

```bash
MESH_BIND_INTERFACE=100.x.x.x   # your $TAILNET_IP
```

Two services pick this up:

- `apps/api` — uvicorn binds to `MESH_BIND_INTERFACE` (falling back to
  `API_HOST`, then `0.0.0.0`).
- `apps/wiki` — Next.js standalone reads the value into its `HOSTNAME`
  env var.

### Step 2 — run containers in host network mode

Docker normally puts containers on a private bridge — they can't see
the host's `tailscale0` interface from there. The simplest fix for a
single-user local setup is host network mode for the public-facing
services.

There are two ways:

**Option A — quick, per-invocation:**

```bash
docker compose run --rm --service-ports --network host api
docker compose run --rm --service-ports --network host wiki
```

**Option B — durable, via a compose override file.**

Create `docker-compose.override.yml` next to the main compose file
(this file is git-ignored by convention; safe to keep local):

```yaml
services:
  api:
    network_mode: host
  wiki:
    network_mode: host
```

Then `make up` works as usual.

### Step 3 — verify from two devices

From the laptop (which is in the tailnet):

```bash
curl http://$TAILNET_IP:8000/healthz   # → {"status":"ok",...}
```

From your phone, in a browser, with Tailscale on:

```
http://your-laptop.tailnet-name.ts.net:3000
```

…should load the wiki. The status page is at
`http://your-laptop.tailnet-name.ts.net:8000/status`.

### Step 4 — verify non-tailnet devices are excluded

Disable Tailscale on a second machine on the same wifi (or use a
phone in airplane-mode + wifi-only-no-VPN). Try:

```bash
curl http://laptop-lan-ip:8000/healthz
```

…should hang or "connection refused". If it succeeds, the bind
interface is wrong — re-check `MESH_BIND_INTERFACE`.

## What's intentionally NOT here

- **App-level auth.** Tailscale is the auth — if you're on the
  tailnet, you have access. Single user, single device class. Adding
  app auth would just be belt-and-suspenders.
- **Public DNS / Cloudflare / Fly / Heroku / Vercel.** Phase 6 is
  local-first. Public deployment is an optional Phase 7+ concern.
- **Tailscale ACLs.** Single user means everyone authorized for the
  tailnet sees everything. ACLs would split that, which we don't
  need.
- **HTTPS termination.** Within a tailnet, traffic is already
  end-to-end encrypted by WireGuard. The API + wiki happily speak
  plain HTTP because they're not on the open internet.

## Operations

- The **status page** at `/status` is the canonical "is the mesh
  healthy?" surface. Meta-refresh every 60s, no JS. Shows last + next
  runs, row counts, agent_tasks status, recent task failures, and
  Langfuse 24h trace count when configured.
- The **scheduler container** (compose profile `scheduler`) keeps the
  mesh ingesting on cron cadence — see [scheduling.md](scheduling.md).
- The **`make backup`** target is the official "back up the DB" path
  if/when added; for now a manual `docker compose exec mesh-postgres pg_dump -U langgraph langgraph > backup.sql`
  is the recommended habit before destructive operations.
