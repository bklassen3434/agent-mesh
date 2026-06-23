#!/usr/bin/env bash
# monitoring/pull-and-report.sh — fetch the Pi's snapshots and render a report.
# Usage: monitoring/pull-and-report.sh [pi@host] [ssh-key]
set -euo pipefail
HOST="${1:-pi@10.0.0.208}"
KEY="${2:-$HOME/.ssh/agentmesh_pi}"
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HERE/../.context/snapshots.jsonl"
mkdir -p "$(dirname "$DEST")"
scp -i "$KEY" -o ConnectTimeout=10 "$HOST:/home/pi/mesh-monitoring/snapshots.jsonl" "$DEST"
python3 "$HERE/analyze.py" "$DEST" "$@"
