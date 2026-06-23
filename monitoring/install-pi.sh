#!/usr/bin/env bash
# monitoring/install-pi.sh — install the hourly metrics snapshot on the Pi.
#
# Idempotent. Copies snapshot.sql to the Pi, drops a self-contained runner +
# crontab entry, and takes one baseline snapshot immediately. Monitoring state
# lives in /home/pi/mesh-monitoring (NOT in the git clone, which the redeploy
# action wipes with `git reset --hard`), so it survives every redeploy + reboot.
#
# Usage: monitoring/install-pi.sh [pi@host] [ssh-key]
set -euo pipefail

HOST="${1:-pi@10.0.0.208}"
KEY="${2:-$HOME/.ssh/agentmesh_pi}"
SSH="ssh -i $KEY -o ConnectTimeout=10"
SCP="scp -i $KEY -o ConnectTimeout=10"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "→ ensuring /home/pi/mesh-monitoring on $HOST"
$SSH "$HOST" 'mkdir -p /home/pi/mesh-monitoring && touch /home/pi/mesh-monitoring/snapshots.jsonl'

echo "→ copying snapshot.sql"
$SCP "$HERE/snapshot.sql" "$HOST:/home/pi/mesh-monitoring/snapshot.sql"

echo "→ writing runner + crontab entry"
$SSH "$HOST" 'bash -s' <<'REMOTE'
set -euo pipefail
cat > /home/pi/mesh-monitoring/run-snapshot.sh <<'RUNNER'
#!/usr/bin/env bash
# Append one JSON metrics line to snapshots.jsonl. Driven by cron (hourly).
set -euo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin
cd /home/pi/agent-mesh   # so `docker compose` finds .env / the running stack
OUT=/home/pi/mesh-monitoring/snapshots.jsonl
LINE=$(docker compose exec -T mesh-postgres psql -U langgraph -d langgraph -tAX \
         < /home/pi/mesh-monitoring/snapshot.sql || true)
# Only append a line that parses as JSON (skips transient DB-down errors).
if printf '%s' "$LINE" | python3 -c 'import sys,json; json.loads(sys.stdin.read())' 2>/dev/null; then
  printf '%s\n' "$LINE" >> "$OUT"
fi
RUNNER
chmod +x /home/pi/mesh-monitoring/run-snapshot.sh

# Install/refresh the hourly cron line (idempotent).
LINE='7 * * * * /home/pi/mesh-monitoring/run-snapshot.sh >> /home/pi/mesh-monitoring/snapshot.log 2>&1'
( crontab -l 2>/dev/null | grep -v 'mesh-monitoring/run-snapshot.sh' ; echo "$LINE" ) | crontab -
echo "crontab now:"; crontab -l | grep mesh-monitoring

# Baseline snapshot right now.
/home/pi/mesh-monitoring/run-snapshot.sh
echo "snapshots so far: $(wc -l < /home/pi/mesh-monitoring/snapshots.jsonl)"
REMOTE

echo "✓ installed. Pull + report with: monitoring/pull-and-report.sh"
