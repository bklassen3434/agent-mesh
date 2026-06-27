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

The deterministic controller (`mesh-controller --apply --forever`) is now the
**sole** orchestrator and runs every skill **in-process** — scouting,
extraction, entity resolution, skeptic, curation, discovery, consolidation. The
old A2A agent servers (all `*-scout` servers, `claim-extractor`,
`entity-tracker`, `sota-tracker`, `curator`, `skeptic`) and the legacy
`scheduler` are **deleted** from the compose files.

So the always-on stack is now just **7 services** (read `docker-compose.yml`):

| Service | Role | ~RAM |
|---|---|---|
| `mesh-postgres` | Postgres + pgvector store (the only data tier) | ~200–400 MB |
| `controller` | always-on self-driving orchestrator (`--forever`, incl. fastembed) | ~250–400 MB |
| `api` | read API on :8000 | ~150 MB |
| `wiki` | Next.js wiki on :3000 | ~150–300 MB |
| `research-qa` | backs the wiki Ask box + Telegram chat (POST /api/v1/ask) | ~250–400 MB |
| `personalizer` | backs the daily brief | ~150–250 MB |
| `telegram-bot` | Telegram bridge | ~80–150 MB |

Add OS + Docker (~300–500 MB) and steady state is roughly **~1.5–2.3 GB**. That
fits in 4 GB, especially with ~8 GB of swap on the SSD to absorb spikes.

So the accommodation is **not** about trimming a big agent fleet — there isn't
one anymore. It is about two things:

1. **Get Postgres off the microSD onto an SSD.** A 24/7 write workload will wear
   out (and eventually corrupt) a microSD card. The SSD is the single most
   important hardware change.
2. **Low-RAM Postgres tuning + swap.** Tune Postgres's memory settings down and
   keep an SSD-backed swap file as a safety net for memory spikes.

There are **no compose profiles** anymore — everything is default-on.

### Scheduled loops are now controller rules, not cron jobs

There is **no scheduler**. The controller self-drives continuously: each pass it
senses the field, plans a worklist via its rule engine, dispatches skills, then
idles `MESH_CONTROLLER_IDLE_SLEEP_SEC` between empty passes. Scouting, skeptic
challenges, discovery, belief consolidation/decay/archival, and memory
consolidation **all still happen** — but as controller rules whose cadence comes
from the rules' own cooldowns plus that idle backoff, not from a cron clock.

---

## Part A — Repo changes (implementation spec)

These are committed to the repo and travel to the Pi via `git pull`. None of
them affect a laptop, because the laptop never sets `COMPOSE_FILE`.

### A1. `docker-compose.pi.yml` — the overlay (lives at repo root)

This is an **opt-in overlay**, not auto-merged. It's already committed — don't
re-create it inline; read the real file. It now does only three things, with
**no compose profiles** (the agent fleet and scheduler are gone, so there's
nothing left to park):

1. **Tunes Postgres for low RAM** and binds its data dir to the SSD.
2. **Binds `mesh_pg_data` to `/mnt/ssd/mesh_pg_data`** (the SSD).
3. **Lengthens healthcheck `start_period` to 180s** for `api`, `wiki`,
   `research-qa`, `personalizer`, and `telegram-bot` — the Pi's arm64 CPU needs
   ~3 min to `uv sync` + import heavy ML deps before binding a port, and the
   base 15–30s gates would otherwise abort `up --build`.

The key bits (see the file for the full thing):

```yaml
# Slow-start anchor merged into every service with a healthcheck (only
# start_period is overridden; the rest is inherited from docker-compose.yml).
x-pi-slow-start: &pi-slow-start
  healthcheck:
    start_period: 180s

services:
  api:           *pi-slow-start
  wiki:          *pi-slow-start
  research-qa:   *pi-slow-start
  personalizer:  *pi-slow-start
  telegram-bot:  *pi-slow-start

  # Postgres: low-RAM tuning + SSD-backed data dir
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

### A2. Makefile — Pi convenience targets (already present)

These targets assume the Pi's `.env` sets `COMPOSE_FILE` so the overlay applies:

```makefile
# ── Raspberry Pi (4 GB) helpers — assume COMPOSE_FILE includes the overlay ──
pi-up:
	docker compose up -d --build --remove-orphans
	@docker compose ps

pi-down:
	docker compose down --remove-orphans

# One bounded controller round (shadow → use controller-apply to act).
pi-pipeline:
	docker compose run --rm --no-deps \
		--entrypoint "uv run mesh-controller --apply" controller
```

`make controller` / `make controller-apply` (in the same Makefile) run one
controller pass against the `controller` service — shadow vs. apply. There is
no `pi-wiki` target anymore: the wiki is always-on.

### A3. Connector enablement is data, not code

Connectors are enabled per-field in the `field_connectors` catalog table — not
in code. The controller polls the enabled ones **in-process**; there is no
per-scout container to start or stop. Disabling a noisy connector is a single
SQL `UPDATE` (Part D, Step 6).

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

**This is the single most important hardware change.** A 24/7 Postgres write
workload **will corrupt and wear out a microSD card**, eventually losing your
whole knowledge base. The SSD is non-negotiable. Use the Pi 5's PCIe NVMe HAT or
a USB 3 SSD.

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

### B2. Swap on the SSD (RAM safety cushion)

Keep ~8 GB of swap **on the SSD** as a free safety net for memory spikes. Never
put swap on the microSD card — the write load that corrupts it for Postgres
applies doubly to swap.

```bash
# An 8 GB swapfile on the SSD, persistent across reboots.
sudo fallocate -l 8G /mnt/ssd/swapfile
sudo chmod 600 /mnt/ssd/swapfile
sudo mkswap /mnt/ssd/swapfile
sudo swapon /mnt/ssd/swapfile
echo '/mnt/ssd/swapfile  none  swap  sw  0  0' | sudo tee -a /etc/fstab
free -h     # confirm ~8 GB of swap now appears
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

# --- Pi accommodation (activates the low-RAM/SSD overlay) ---
COMPOSE_FILE=docker-compose.yml:docker-compose.pi.yml
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
> Pi (e.g. `ufw`) so ports 3000/8000/8013/8016/9110 aren't exposed on your LAN.

### Step 3 — First build & boot

The simplest way to start everything is the launcher at the repo root. It
respects `COMPOSE_FILE`, so on the Pi it picks up the overlay automatically:

```bash
./run.sh                         # build (if needed) + start all 7 services, then show status
```

`make pi-up` and `docker compose up -d --build --remove-orphans` are exact
equivalents. The first arm64 build is slow (10–20 min, including the Next.js
wiki build) — be patient.

### Step 4 — Apply schema + roles

```bash
uv run mesh.cli init-db          # idempotent; creates the knowledge schema + writer/reader roles
./run.sh status                  # confirm all 7 services are healthy (or: docker compose ps)
```

### Step 5 — Verify the pipeline end-to-end

```bash
curl -s localhost:8000/healthz
make pi-pipeline                 # one bounded controller run via the `controller` service
uv run mesh.cli pipeline-stats   # confirm claims / entities / beliefs landed
```

(The always-on `controller` service is already self-driving; `make pi-pipeline`
just runs one extra bounded pass on demand.)

### Step 6 — (Optional) Disable noisy connectors

The controller polls every enabled connector for the field **in-process**. If a
connector is failing or you want to cut load, disable it in the catalog — no
container to stop:

```bash
docker compose exec mesh-postgres psql -U langgraph -d langgraph -c \
"UPDATE knowledge.field_connectors SET enabled = false
 WHERE field_id = 'ai-robotics'
   AND connector_id IN ('bluesky','reddit','blog','leaderboard');"
```

Re-enable later by flipping `enabled = true` — that's the whole change.

### Step 7 — Confirm always-on + reboot persistence

```bash
docker stats --no-stream         # steady state ~1.5–2.3 GB, peak well under 4 GB
free -h
sudo reboot                      # after reboot, all 7 services auto-restart
# (restart: unless-stopped + `systemctl enable docker`). The controller resumes
# self-driving with no laptop involved.
```

---

## Part E — Operations

### Browse the wiki

The wiki is always-on — just open it (no separate start step):

```bash
# → http://<pi-name>.<tailnet>.ts.net:3000
```

### Follow what the controller is doing

```bash
./run.sh logs                    # follow logs from every service (or: docker compose logs -f)
docker compose logs -f controller
```

### Tune the cadence

There is **no scheduler** and no `schedules` table to edit. The controller
self-drives; cadence comes from the rules' own cooldowns plus idle backoff. Tune
it through the `MESH_CONTROLLER_*` env vars in `.env`, then restart the
controller:

```bash
# e.g. scout connectors less often, idle longer between empty passes:
#   MESH_CONTROLLER_SCOUT_COOLDOWN_SEC=...   MESH_CONTROLLER_IDLE_SLEEP_SEC=...
#   MESH_CONTROLLER_MAINTAIN_COOLDOWN_SEC=...
docker compose up -d controller   # picks up the new .env values
```

To cut work without touching cadence, disable a connector in the catalog
(Part D, Step 6).

### Run the controller manually

The always-on `controller` already runs continuously. To force one extra bounded
pass on demand (extract, challenge, consolidate, discover — whichever rules have
pending tensions):

```bash
make controller-apply    # one full controller pass against the `controller` service
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
| Container exits with **code 137** | OOM-kill. The stack is already minimal (7 in-process services — there's nothing to trim). Add/confirm ~8 GB SSD swap (B2), confirm Postgres is on the SSD with the low-RAM tuning applied (overlay active via `COMPOSE_FILE`), and check `free -h` / `docker stats`. Stretch `MESH_CONTROLLER_*` cooldowns to spread load. |
| Controller logs a connector reach failure | Disable that connector for the field (Step 6). |
| Postgres won't start, permission errors on data dir | `/mnt/ssd/mesh_pg_data` not writable / SSD not mounted. Confirm `mount -a` and the dir exists. |
| Wiki build killed during `up --build` | The arm64 Next.js build is the heaviest step. Confirm SSD swap is on (B2); if it still OOMs, build it on a laptop with `docker buildx --platform linux/arm64`, push/load the image, then `docker compose up -d wiki`. |
| Everything dies on reboot | `sudo systemctl enable docker`; confirm services show `restart: unless-stopped` in `docker inspect`. |

The stack is already at its minimal footprint: the deterministic controller runs
every skill **in-process**, so there are no agent containers to stop or
"low-footprint mode" to switch to. The only knobs left are swap, the SSD, the
Postgres tuning (all above), and the `MESH_CONTROLLER_*` cooldowns.

---

## Appendix — Connectors

There are no per-scout containers. Connectors are enabled per-field in the
`knowledge.field_connectors` catalog table, and the controller polls each
enabled one **in-process** during scouting. Enable/disable a connector with a
single SQL `UPDATE` (Part D, Step 6) — no container starts or stops.
