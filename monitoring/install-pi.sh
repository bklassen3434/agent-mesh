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

echo "→ copying snapshot.sql + run-snapshot.sh"
$SCP "$HERE/snapshot.sql"     "$HOST:/home/pi/mesh-monitoring/snapshot.sql"
$SCP "$HERE/run-snapshot.sh"  "$HOST:/home/pi/mesh-monitoring/run-snapshot.sh"

echo "→ installing crontab entry + baseline snapshot"
# Single flat command (no nested heredoc — that bailed early under set -e). The
# crontab line uses `;` so the grep-no-match exit code can't trip the pipeline.
$SSH "$HOST" '
  set -uo pipefail
  chmod +x /home/pi/mesh-monitoring/run-snapshot.sh
  LINE="7 * * * * /home/pi/mesh-monitoring/run-snapshot.sh >> /home/pi/mesh-monitoring/snapshot.log 2>&1"
  ( crontab -l 2>/dev/null | grep -v "mesh-monitoring/run-snapshot.sh"; echo "$LINE" ) | crontab -
  echo "crontab now:"; crontab -l | grep mesh-monitoring
  /home/pi/mesh-monitoring/run-snapshot.sh
  echo "snapshots so far: $(wc -l < /home/pi/mesh-monitoring/snapshots.jsonl)"
'

echo "✓ installed. Pull + report with: monitoring/pull-and-report.sh"
