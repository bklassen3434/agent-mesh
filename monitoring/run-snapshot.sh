#!/usr/bin/env bash
# Append one JSON metrics line to snapshots.jsonl. Driven by cron (hourly).
# Lives on the Pi at /home/pi/mesh-monitoring/ (copied by install-pi.sh).
set -uo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin
cd /home/pi/agent-mesh   # so `docker compose` finds .env / the running stack
DIR=/home/pi/mesh-monitoring
OUT=$DIR/snapshots.jsonl
LINE=$(docker compose exec -T mesh-postgres psql -U langgraph -d langgraph -tAX \
         < "$DIR/snapshot.sql" || true)
# Only append a line that parses as JSON (skips transient DB-down errors).
if printf '%s' "$LINE" | python3 -c 'import sys,json; json.loads(sys.stdin.read())' >/dev/null 2>&1; then
  printf '%s\n' "$LINE" >> "$OUT"
fi
