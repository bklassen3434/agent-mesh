# Raspberry Pi 5 (4 GB) Deployment Runbook

A complete, self-contained guide to running Agent Mesh **always-on** on a
Raspberry Pi 5 with **4 GB of RAM**, replacing the "laptop is production" model.
The Pi never sleeps, sips ~5–10 W, and stays reachable over Tailscale.

This document has two audiences:

- **Part A — Repo changes** is the implementation spec: files to create/verify
  in the codebase. A fresh agent can execute these directly.
- **Part B onward** is the host runbook: what a human runs on the Pi.

> **Read this first — the one rule.** This stack does **not** run LLM inference
> locally by default. With `MESH_LLM_PROVIDER=anthropic` (the default), the Pi
> only does orchestration, embeddings (small `fastembed` ONNX model on CPU),
> Postgres, and HTTP. Anthropic does the thinking. **Do not point this at a
> local Ollama on the Pi** — an 8B model on a Pi 5 CPU is unusably slow. Keep
> `MESH_LLM_PROVIDER=anthropic`.

---

## Why a 4 GB Pi needs accommodation

`make up` (plain `docker compose up -d`) starts ~18 long-running containers:
10 scout HTTP servers, `personalizer`, `research-qa`, `claim-extractor`,
`entity-tracker`, `sota-tracker`, `api`, `wiki`, plus `mesh-postgres`. Steady
state is ~4–4.5 GB — over budget on a 4 GB box, and the Next.js wiki build can
OOM during `npm run build`.

The accommodation **trims the always-on set without losing the modern A2A
pipeline**. The only functional reduction is fewer source connectors
(`arxiv` + `github` + `hn` instead of all 7).

### Target footprint

| State | Services | ~RAM |
|---|---|---|
| **Always-on** | `mesh-postgres`, `scheduler`, `api`, `arxiv-scout`, `github-scout`, `hn-scout`, `claim-extractor`, `entity-tracker`, `sota-tracker`, `curator`, `skeptic` | ~1.9 GB idle / ~2.7 GB peak |
| **On-demand** | `wiki`, `personalizer`, `research-qa`, unused scouts | started only when needed |

All five scheduled loops keep working: `ingest` (6 h), `skeptic`,
`discovery`, `belief_consolidation`, `memory_consolidation` (all daily).

---

## Part A — Repo changes (implementation spec)

These are committed to the repo and travel to the Pi via `git pull`. None of
them affect a laptop, because the laptop never sets `COMPOSE_FILE`.

### A1. `docker-compose.pi.yml` — the overlay (create at repo root)

This is an **opt-in overlay**, not auto-merged. Verify it exists with exactly
this content (it may already be present):

```yaml
# docker-compose.pi.yml — Raspberry Pi 5 (4 GB) accommodation overlay.
#
# NOT auto-merged. Opt in on the Pi only, either by passing it explicitly:
#     docker compose -f docker-compose.yml -f docker-compose.pi.yml up -d
# or (recommended) by setting in the Pi's .env:
#     COMPOSE_FILE=docker-compose.yml:docker-compose.pi.yml
# so every `docker compose` / `make` command picks it up automatically.
# Your laptop never sets COMPOSE_FILE, so its behaviour is unchanged.

services:
  # ── scouts we don't run on the Pi → park in the `extra` profile ────────────
  bluesky-scout:     { profiles: ["extra"] }
  reddit-scout:      { profiles: ["extra"] }
  blog-scout:        { profiles: ["extra"] }
  leaderboard-scout: { profiles: ["extra"] }
  web-search-scout:  { profiles: ["extra"] }
  rss-scout:         { profiles: ["extra"] }
  rest-json-scout:   { profiles: ["extra"] }

  # ── viewing/aux services → `ui` profile (start only when you need them) ─────
  wiki:         { profiles: ["ui"] }
  personalizer: { profiles: ["ui"] }
  research-qa:  { profiles: ["ui"] }

  # ── Postgres: low-RAM tuning + SSD-backed data dir ─────────────────────────
  mesh-postgres:
    command:
      - postgres
      - -c
      - max_connections=50
      - -c
      - shared_buffers=128MB
      - -c
      - effective_cache_size=384MB
      - -c
      - work_mem=8MB
      - -c
      - maintenance_work_mem=64MB

  # ── scheduler: only advertise the agents we actually run on the Pi ─────────
  scheduler:
    environment:
      MESH_AGENT_URLS: "http://arxiv-scout:8001,http://claim-extractor:8002,http://entity-tracker:8003,http://sota-tracker:8004,http://hn-scout:8005,http://github-scout:8008"
      MESH_SKEPTIC_AGENT_URLS: "http://curator:8007,http://skeptic:8006"

# Bind the named Postgres volume to a directory on the SSD.
volumes:
  mesh_pg_data:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /mnt/ssd/mesh_pg_data
```

> If the SSD mount path differs from `/mnt/ssd`, change `device:` accordingly.

### A2. Makefile — add Pi convenience targets

Append these targets (tab-indented recipes). They assume the Pi's `.env` sets
`COMPOSE_FILE` so the overlay applies. Add `pi-up pi-down pi-wiki pi-pipeline`
to the `.PHONY` line.

```makefile
# ── Raspberry Pi (4 GB) helpers — assume COMPOSE_FILE includes the overlay ──
pi-up:
	docker compose up -d --build
	@docker compose ps

pi-down:
	docker compose --profile ui --profile extra down --remove-orphans

# Browse the wiki on demand, then `docker compose stop wiki` to free the RAM.
pi-wiki:
	docker compose up -d wiki
	@echo "wiki → http://localhost:3000 (or the Pi's tailnet name)"

# One bounded pipeline run via the scheduler image (PAPERS defaults to 5).
pi-pipeline:
	docker compose run --rm --no-deps \
		--entrypoint "uv run mesh-ingest --a2a --max-papers $${PAPERS:-5}" scheduler
```

### A3. (Optional) Scheduler misfire grace — `apps/scheduler/src/mesh_scheduler/scheduler.py`

In `SchedulerManager._register`, the `add_job(...)` call already sets
`coalesce=True, max_instances=1`. Add a generous `misfire_grace_time` so a
within-process pause (a long-running job, a brief hang) doesn't silently drop
the next fire:

```python
        self._scheduler.add_job(
            self._scheduled_fire,
            args=[job_id, field_id],
            trigger=IntervalTrigger(hours=hours),
            id=aps_id,
            name=f"Mesh {aps_id}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,  # tolerate up to 1h of lateness, then run once
        )
```

**Honest scope:** this helps *within* a running process. It does **not** make
the scheduler "catch up" on fires missed while the whole process/host was down,
because the jobstore is in-memory and rebuilt on startup (next run = now +
interval). For the Pi that's acceptable: after a reboot, the first run lands
within one interval (≤6 h for the pipeline), and every job is idempotent. Full
reboot-catch-up would require a persistent jobstore or a startup "run if
overdue" check against `pipeline_runs.started_at` — out of scope here; only do
it if missing one post-reboot cycle is genuinely unacceptable.

### A4. Connector disablement is data, not code

The unused connectors are turned off in the `field_connectors` table at deploy
time (Part D, Step 6) — not in code. No repo change. Re-enabling later is a
single SQL `UPDATE` plus starting that scout.

---

## Part B — Host preparation (one-time, on the Pi)

Use **64-bit Raspberry Pi OS** (Bookworm or newer).

```bash
# Docker + compose plugin
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"          # then log out / back in
sudo systemctl enable docker             # start the stack on every boot
docker --version && docker compose version

# uv (for the CLI / init-db)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### B1. SSD for Postgres (required — do not use the microSD card)

A microSD card is slow under Postgres's write pattern and **will wear out**
under a 24/7 DB workload, eventually losing your knowledge base. Use the Pi 5's
PCIe NVMe HAT or a USB 3 SSD.

```bash
# Mount the SSD at /mnt/ssd and make it persistent across reboots.
sudo mkdir -p /mnt/ssd
# Find the device + UUID:
lsblk -f
# Add to /etc/fstab (replace UUID and fs type), e.g.:
#   UUID=xxxx-xxxx  /mnt/ssd  ext4  defaults,noatime  0  2
sudo mount -a

# Create the directory the overlay binds the Postgres volume to:
sudo mkdir -p /mnt/ssd/mesh_pg_data
```

### B2. zram compressed swap (RAM safety cushion)

Absorbs the brief peak during a pipeline run without thrashing the SSD.

```bash
sudo apt-get install -y zram-tools
printf 'ALGO=zstd\nPERCENT=50\n' | sudo tee /etc/default/zramswap
sudo systemctl restart zramswap
free -h     # confirm a swap line now appears
```

### B3. Docker log rotation (prevent disk fill)

24/7 containers can fill the disk with JSON logs. Cap them globally:

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
sudo systemctl restart docker
```

---

## Part C — Tailscale access (recommended)

Keep the Pi off the public internet; reach it from your devices over the tailnet
(mirrors `docs/deployment.md`, with the Pi as host).

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4        # note the 100.x.x.x address → use as MESH_BIND_INTERFACE
```

You then reach the wiki at `http://<pi-name>.<tailnet>.ts.net:3000` and the API
at `:8000`, with nothing exposed to the public internet.

---

## Part D — Configure & launch

### Step 1 — Clone and select the overlay

```bash
git clone <your-repo-url> agent-mesh && cd agent-mesh
git pull                    # ensure docker-compose.pi.yml + Makefile targets are present
cp .env.example .env
```

### Step 2 — Edit `.env` (set ALL of these before the first `up`)

```bash
# --- LLM: Pi orchestrates, Anthropic infers ---
ANTHROPIC_API_KEY=sk-ant-...
MESH_LLM_PROVIDER=anthropic

# --- Pi accommodation (activates the overlay + trimmed always-on set) ---
COMPOSE_FILE=docker-compose.yml:docker-compose.pi.yml
COMPOSE_PROFILES=scheduler,skeptic
MESH_PG_POOL_MAX=3

# --- HARDENING: change the default DB passwords BEFORE first init ---
# (POSTGRES_PASSWORD only takes effect on the first volume init — set it now.)
LANGGRAPH_POSTGRES_PASSWORD=<strong-random>
MESH_WRITER_PASSWORD=<strong-random>
MESH_READER_PASSWORD=<strong-random>

# --- Tailscale-only access (recommended) ---
MESH_BIND_INTERFACE=100.x.x.x        # the Pi's tailnet IP from `tailscale ip -4`
```

> If you skip Tailscale, leave `MESH_BIND_INTERFACE` empty **and** firewall the
> Pi (e.g. `ufw`) so ports 3000/8000/9100/8001+ aren't exposed on your LAN.

### Step 3 — First build & boot

```bash
docker compose up -d --build     # or: make pi-up
# First arm64 build is slow (10–20 min). Be patient. The heavy Next.js wiki
# build is skipped (it's in the `ui` profile).
```

### Step 4 — Apply schema + roles

```bash
uv run mesh.cli init-db          # idempotent; creates the knowledge schema + writer/reader roles
docker compose ps                # confirm the always-on set is healthy
```

### Step 5 — Verify the pipeline end-to-end

```bash
curl -s localhost:8000/healthz
make pi-pipeline                 # one bounded 5-paper run via the scheduler image
uv run mesh.cli pipeline-stats   # confirm claims / entities / beliefs landed
```

### Step 6 — Disable the unused connectors

The trimmed scouts aren't running, but `ai-robotics` still enables all 7
connectors, so the coordinator would try (and fail, noisily) to reach the
missing ones. Turn them off:

```bash
docker compose exec mesh-postgres psql -U langgraph -d langgraph -c \
"UPDATE knowledge.field_connectors SET enabled = false
 WHERE field_id = 'ai-robotics'
   AND connector_id IN ('bluesky','reddit','blog','leaderboard');"
```

Leaves `arxiv`, `hn`, `github` enabled — matching the running scouts.

### Step 7 — Confirm always-on + reboot persistence

```bash
docker stats --no-stream         # peak should stay well under 4 GB
free -h
sudo reboot                      # after reboot, the always-on set auto-restarts
# (restart: unless-stopped + `systemctl enable docker`). The scheduler resumes
# its clock with no laptop involved.
```

---

## Part E — Operations

### Browse the wiki on demand

```bash
make pi-wiki                     # builds + starts the wiki (first build is slow)
# → http://<pi-name>.<tailnet>.ts.net:3000
docker compose stop wiki         # free the RAM when done
```

### Adjust the schedule (no restart needed)

```bash
# Stretch the ingest loop to every 12h to reduce load:
docker compose exec mesh-postgres psql -U langgraph -d langgraph -c \
"UPDATE public.schedules SET interval_hours = 12 WHERE job_id = 'ingest';"
# Disable a loop entirely:
docker compose exec mesh-postgres psql -U langgraph -d langgraph -c \
"UPDATE public.schedules SET enabled = false WHERE job_id = 'discovery';"
# Applies within ~30s via the scheduler's reconcile poll.
```

### Run maintenance loops manually (if you dropped the `skeptic` profile)

If you remove `skeptic` from `COMPOSE_PROFILES` to save ~350 MB, disable the
`skeptic`/`discovery` schedules and run them by hand — each spins the
needed agents up, runs, and exits:

```bash
make skeptic             # curator + skeptic + one falsification sweep
make consolidate-beliefs # semantic belief dedup + decay/archival
make discover            # autonomous gap analysis → discovery investigations
```

### Nightly backup of the knowledge store (recommended)

```bash
# Add to crontab (crontab -e), dumping to the SSD:
0 4 * * *  docker compose -f /home/pi/agent-mesh/docker-compose.yml exec -T mesh-postgres \
  pg_dump -U langgraph -d langgraph -n knowledge \
  | gzip > /mnt/ssd/backups/mesh-$(date +\%F).sql.gz
```

(Create `/mnt/ssd/backups` first; prune old dumps with a `find ... -mtime +14 -delete`.)

---

## Part F — Troubleshooting & fallback

| Symptom | Cause / fix |
|---|---|
| Container exits with **code 137** | OOM-kill. Drop the `skeptic` profile, or move to in-process mode (below). |
| Pipeline logs "card discovery failed" for a scout | That scout isn't running. Either disable its connector (Step 6) or start it: `docker compose up -d <name>-scout`. |
| Postgres won't start, permission errors on data dir | `/mnt/ssd/mesh_pg_data` not writable / SSD not mounted. Confirm `mount -a` and the dir exists. |
| Wiki build killed | Build it on a laptop with `docker buildx --platform linux/arm64`, push/load the image, then `docker compose up -d wiki`. |
| Everything dies on reboot | `sudo systemctl enable docker`; confirm services show `restart: unless-stopped` in `docker inspect`. |

### Break-glass fallback: in-process mode (zero agent containers)

If 4 GB is still too tight even after trimming, run the legacy in-process
orchestrator: agents run inside one process, so **only `mesh-postgres` +
`scheduler` + `api`** need to be up.

Change the scheduler's ingest command from `mesh-ingest --a2a` to
`mesh-ingest` (drop `--a2a`) — edit `JOB_COMMANDS["ingest"]` in
`apps/scheduler/src/mesh_scheduler/scheduler.py`, and stop the scout/extractor
containers.

**Cost:** the in-process orchestrator is the Phase-1 path — **arxiv-only**, no
per-field connectors, no semantic entity resolution, no discovery, no
observability capture. Treat it as a last resort, not the default.

---

## Appendix — Connector slug ↔ scout container reference

| Connector slug | Scout container | Port | Default-enabled (ai-robotics) | Kept on Pi |
|---|---|---|---|---|
| `arxiv` | `arxiv-scout` | 8001 | ✅ | ✅ |
| `hn` | `hn-scout` | 8005 | ✅ | ✅ |
| `github` | `github-scout` | 8008 | ✅ | ✅ |
| `bluesky` | `bluesky-scout` | 8009 | ✅ | ❌ (disabled) |
| `reddit` | `reddit-scout` | 8010 | ✅ | ❌ (disabled) |
| `blog` | `blog-scout` | 8011 | ✅ | ❌ (disabled) |
| `leaderboard` | `leaderboard-scout` | 8012 | ✅ | ❌ (disabled) |
| (config-driven) | `web-search-scout` | 8017 | ❌ | ❌ |
| (config-driven) | `rss-scout` | 8014 | ❌ | ❌ |
| (config-driven) | `rest-json-scout` | 8015 | ❌ | ❌ |

Other always-on (no connector): `claim-extractor` (8002), `entity-tracker`
(8003), `sota-tracker` (8004), `curator` (8007), `skeptic` (8006), `api`
(8000), `scheduler` (9100), `mesh-postgres` (5432).
