# monitoring/ — Pi performance tracking

Lightweight, append-only time-series of the live system's health, so we can tell
whether the always-on Pi deployment is actually doing well and tune it.

No app code, no image rebuild: a cron job on the Pi runs `snapshot.sql` against
the running Postgres every hour and appends one JSON line to
`/home/pi/mesh-monitoring/snapshots.jsonl` (in `$HOME`, so it survives every
`git reset --hard` redeploy and every reboot).

## What's captured (each maps to a tuning knob)

- **kb** — entities / sources / claims (by status & type) / beliefs / relationships
  / investigations (by status & origin). *Is scouting + synthesis producing anything?*
- **quality** — held-belief confidence (avg/min/max + bands), avg supporting
  claims per belief, contradicting links, skeptic counter-claims.
  *→ `MESH_CONFIDENCE_*`, merge bands, adjudication thresholds.*
- **controller** — agent invocations (total / 24h / 1h), distinct runs, by-skill,
  by-status, errors + error types, last-invocation watermark.
  *→ `MESH_CONTROLLER_STEP_CAP` / `ESCALATE_AFTER` / cooldowns; is the scheduler firing?*
- **cost** — LLM spend total + 24h, tokens, split by model (routing tier) and skill.
  *→ routing config, `MESH_PIPELINE_MAX_PAPERS`, model pins.*
- **liveness** — last claim / source / belief-revision timestamps.

## Operate

```bash
# One-time install (also takes a baseline snapshot):
monitoring/install-pi.sh                 # defaults to pi@10.0.0.208, ~/.ssh/agentmesh_pi

# Any time you want to see how it's doing:
monitoring/pull-and-report.sh            # scp the jsonl → .context/ and print trends + flags
python3 monitoring/analyze.py .context/snapshots.jsonl --last 50
```

`analyze.py` prints a trend table, the latest full snapshot, and automatic flags
(controller idle, skill errors, claims-but-no-beliefs, low confidence).

## Baseline (2026-06-22, pre-redeploy)

Old telegram-branch code: 6 entities, 18 claims, **0 beliefs**, last controller
activity 2026-06-14 (stalled). Post-redeploy to controller-only orchestration we
expect invocations every 6h and beliefs > 0.
