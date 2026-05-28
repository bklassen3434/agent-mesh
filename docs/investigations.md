# Investigations

Phase 7a turned the mesh from purely reactive (scout → extract → believe)
into something that can ask its own follow-up questions. Investigations
are the mechanism.

## Lifecycle

```
                    Curator                       Coordinator
                       │                               │
        stale belief ──┤                               │
                       ▼                               │
        InvestigationSuggestion                        │
                       │                               │
                       ▼                               │
                  ┌─────────┐  next pipeline run  ┌────────────┐
                  │ open    │ ─────────────────► │ in_progress │
                  └─────────┘                     └─────┬──────┘
                                                       │
                                      claims ≥ T ──────┼────── runs ≥ N
                                                       │
                                                       ▼
                                                  ┌──────────┐
                                                  │ resolved │
                                                  └──────────┘
                                                       │
                                                  ┌──────────┐
                                                  │ abandoned│
                                                  └──────────┘
```

## Who does what

- **Curator** (rule-based, no LLM) reads every held belief and emits an
  `InvestigationSuggestion` when any of these signal: thin evidence
  (<2 supporters, >14 days old), stale evidence (>60 days since last
  claim), no claims at all, recent contradicting activity.
- **Skeptic sweep** persists the suggestions as `Investigation` rows.
  Skips beliefs that already have an open or in_progress investigation
  so the same stale belief doesn't spawn duplicates.
- **Coordinator** on each pipeline run queries open + in_progress
  investigations, transitions each to `in_progress`, dispatches to
  scouts whose source type matches `suggested_source_types`, and
  threads returned source_records through the normal dedup → insert →
  extract pipeline. Claims that fall out of investigation-borne sources
  get attached to the investigation via `attach_claim_to_investigation`.
- **Coordinator** also runs the lifecycle sweep at the end: resolve
  when `len(collected_claim_ids) ≥ MESH_INVESTIGATION_CLAIMS_THRESHOLD`,
  abandon when `pipeline_runs_attempted ≥ MESH_INVESTIGATION_MAX_RUNS`.

## Scouts

Each of the seven scouts advertises an `investigate_<source>` skill
alongside its existing `scout_<source>`. Only `investigate_arxiv` runs
a real hypothesis-directed query in 7a — it uses arxiv's free-text
search API with the hypothesis as the query. The other six advertise
the skill via `make_empty_investigate_handler` so capability discovery
sees them but they return zero results. Filling in deeper search per
scout is a follow-up; the arxiv path is enough to demonstrate
end-to-end lifecycle.

## Inspecting

```bash
uv run mesh.cli investigations list
uv run mesh.cli investigations list --status open
uv run mesh.cli investigations list --status resolved
```

Each row shows status, target_entity_id, opened_by_belief_id,
suggested source types, `pipeline_runs_attempted`,
`len(collected_claim_ids)`, and the hypothesis.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `MESH_INVESTIGATION_CLAIMS_THRESHOLD` | `3` | Collected claims needed to mark resolved. |
| `MESH_INVESTIGATION_MAX_RUNS` | `5` | Pipeline runs before an empty investigation is abandoned. |

## Out of scope (deferred to Phase 7b+)

- A wiki UI surface for investigations.
- User-initiated investigations (currently Curator-opened only).
- Investigation prioritization beyond FIFO discovery order.
- Deep per-source investigate implementations beyond arxiv (the other
  six advertise the skill but return empty).
- Skipping reproduction signal claim → investigation linkage (Phase 7b
  exploration will inform whether the linkage gets richer).
