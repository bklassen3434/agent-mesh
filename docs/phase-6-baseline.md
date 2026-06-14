# Phase 6 baseline (pre-6a)

Snapshot taken at the start of Phase 6 work, against the local
`./data/mesh.db`. Used to verify that 6a's only effect on the DB is
adding scheduled `pipeline_runs` rows — no structural drift.

## Counts

| Entity | Count |
|---|---|
| `pipeline_runs` (`run_type='ingest'`) | 0 |
| `pipeline_runs` (`run_type='skeptic'`) | 1 |
| `claims` | 0 |
| `beliefs` | 0 |
| `belief_revisions` | 0 |
| `sources` | 0 |
| `entities` | 0 |

## Notes

The local dev DB is essentially empty — one skeptic sweep ran against
zero beliefs. This is fine for 6a verification (we care about the
*triggered_by* tagging and scheduled-run cadence, not row counts). 6a
exit criteria will validate against fresh scheduled runs landing here
with `triggered_by='scheduled'`.

## Post-6a expected delta

Only new column `triggered_by` on `pipeline_runs`, plus N new rows
with `triggered_by='scheduled'`. No schema changes to other tables.
