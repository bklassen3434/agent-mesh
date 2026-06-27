#!/usr/bin/env bash
# run.sh — one command to launch the whole Agent Mesh stack and keep it 24/7.
#
#   ./run.sh           build (if needed) + start everything, then show status
#   ./run.sh stop      stop the stack (containers stay, data is kept)
#   ./run.sh down      stop + remove containers (the Postgres volume is kept)
#   ./run.sh logs      follow logs from every service
#   ./run.sh status    show what's running
#
# On the Raspberry Pi, set this once in .env so the low-RAM/SSD overlay is
# always applied:  COMPOSE_FILE=docker-compose.yml:docker-compose.pi.yml
# `restart: unless-stopped` on every service means the stack comes back by
# itself after a reboot (as long as Docker starts on boot:
#   sudo systemctl enable docker).
set -euo pipefail

cd "$(dirname "$0")"

# Pick `docker compose` (v2) or the legacy `docker-compose`.
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  echo "ERROR: Docker Compose not found. Install Docker, then re-run." >&2
  exit 1
fi

cmd="${1:-up}"

case "$cmd" in
  stop)   exec $DC stop ;;
  down)   exec $DC down --remove-orphans ;;
  logs)   exec $DC logs -f ;;
  status|ps) exec $DC ps ;;
  up|"") ;;  # fall through to the start path below
  *) echo "Usage: ./run.sh [up|stop|down|logs|status]" >&2; exit 1 ;;
esac

# First-run guard: you need a .env (mainly ANTHROPIC_API_KEY for the LLM).
if [ ! -f .env ]; then
  echo "→ No .env found — creating one from .env.example."
  cp .env.example .env
  echo "  Edit .env and set ANTHROPIC_API_KEY before the controller can do useful work."
fi

if ! grep -qE '^ANTHROPIC_API_KEY=.+' .env 2>/dev/null; then
  echo "⚠  ANTHROPIC_API_KEY looks unset in .env."
  echo "   The stack will still start, but the controller can't call the LLM until you set it."
fi

echo "→ Building and starting the stack (this can take a few minutes on first run / on the Pi)..."
# --remove-orphans clears out any containers from services that no longer exist
# (e.g. the old scout / extractor / scheduler containers that were removed).
$DC up -d --build --remove-orphans

echo
echo "→ Status:"
$DC ps
echo
echo "Done. The controller is now self-driving 24/7. Open:"
echo "  wiki  →  http://localhost:3000"
echo "  api   →  http://localhost:8000/docs"
echo
echo "Follow what it's doing:   ./run.sh logs"
echo "Stop it:                  ./run.sh stop"
