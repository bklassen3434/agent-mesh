# Episodic agent memory (Phase 15)

Phase 15 is a **measurement phase**, the first concrete step toward agent
working memory. It assembles the episodic action history that is *already
implicitly persisted* across the existing knowledge tables into a first-person,
queryable form — and (15b) labels each entry with its downstream outcome.
**Nothing new is written, no new table is created, and no agent behavior
changes.** The goal is to learn empirically how much agent episodic memory is
reconstructable from existing state before any decision to build a new store.

- **Episodic memory** = an agent's first-person, time-ordered record of *what it
  did* (which claims it extracted, which beliefs it revised).
- **World-model** = the `Claim`/`Belief` facts recording what is *true about the
  field*.

Same underlying data, different lens. The read model lives in
`packages/mesh-db/src/mesh_db/episodic.py` (`recall_history`), a query-only
module in the style of `graph.py`.

## 15a — the read model

### The falsification question (read step 1)

> Is "which agent did this, on which run, via which skill" cleanly recoverable?

**Yes — for agents that write agent-attributed artifacts.** Note that the
`agent_task_events` table referenced by the original phase brief no longer
exists (it was dropped in Phase 8 when orchestration moved to LangGraph
checkpoints). The recoverable attribution today lives on the artifacts
themselves:

| Event source | Table / column | Agents |
|---|---|---|
| Claim extraction | `claims.extracted_by_agent` | `claim_extractor`, `skeptic` |
| Belief revision  | `belief_revisions.revised_by_agent` | `sota_tracker`, `synthesizer`, `skeptic` |

`recall_history` is built on exactly these two sources. It is **not** a STOP:
per-agent history is genuinely reconstructable for the agents whose work product
is attributed.

### Two reconstructions (surfaced, not hidden)

- **Run linkage is by timestamp containment.** Claims and belief revisions carry
  a timestamp but **no `run_id`**. Each event's run is recovered by joining to
  the newest `pipeline_runs` row whose `[started_at, finished_at]` window
  contains the artifact timestamp. This is sound because the coordinator owns all
  writes and a job never runs concurrently with itself, so an artifact falls in
  exactly one same-type run window in practice. `run_id` is `None` only when no
  window contains the timestamp (e.g. a crashed run with no finalize row, or
  pre-run seed data).
- **`skill` is derived, not stored.** Per-artifact skill id is not persisted, so
  it is derived deterministically from `(event_type, agent)` (`extract_claims`,
  `challenge_belief`, `update_sota`, `synthesize_capability`). Unmapped authors
  fall back to the agent name.

### Honest negatives (gaps, not bugs)

- **Scout → source production is not per-agent reconstructable.** `sources`
  carry no agent attribution. The only exception is the Skeptic's synthetic
  `agent_reasoning` sources (`author = 'skeptic'`, `url = agent://skeptic/...`).
- **Belief *creation* is not a timestamped agent event.** `beliefs` has no
  `created_at` / `created_by`; only *revisions* (`belief_revisions`) are
  attributed and timestamped. Synthesis creation activity is therefore invisible
  to the episodic log.
- **Investigations are not agent-attributed.** `investigations.assigned_scout_agents`
  is declared but **never written** (read only by the CLI display). Investigations
  are therefore not a standalone agent event source; they enter the model only as
  an *outcome* dimension in 15b (a produced claim attached to an investigation,
  and that investigation's resolved/abandoned/open fate).

### Retrieval API

```python
recall_history(
    conn, agent,
    *, entity_id=None, source_id=None, topic=None,
    since=None, until=None, limit=50,
) -> list[EpisodicEntry]
```

Returns merged, most-recent-first `{run_id, timestamp, agent, skill,
event_type, action_summary, refs}` entries. Scope filters apply per source where
meaningful: `entity_id` (extraction subject, or a revision's capability belief /
trigger-claim subjects); `source_id` (extraction only — revisions have no
source, so they are excluded when it is set); `topic` (revision belief topic
only — extractions excluded when it is set); `since`/`until` (inclusive window);
`limit` (capped at `MAX_LIMIT = 200`). Read-only: only SELECTs, no writes.

## A2A exposure (read step 4) — STOPPED by design

The brief asked to expose `recall_history` as a read-only A2A skill *via the
existing capability-discovery mechanism*, with an explicit instruction to **STOP
and report rather than stand up a new service** if no natural host exists.

**No natural host exists.** Every mesh agent server is deliberately *stateless*:
none opens a DB connection inside its skill handlers. The coordinator is the sole
DB owner, and this is enforced by the `mesh_writer` / `mesh_reader` Postgres
roles. Exposing a DB-reading `recall_history` skill would require either:

1. a new dedicated "historian" agent server/container — a **new service**, which
   the phase forbids; or
2. giving an existing stateless agent a reader DB connection plus an unrelated
   query skill — which breaks the stateless-agent invariant (not a *clean*
   exposure).

Per the brief's STOP clause, **the A2A skill was not built.** The substantive
deliverable — the reconstructed, queryable read model — stands on its own as
`mesh_db.episodic.recall_history`, validated by `tests/test_episodic.py`. Agent
*consumption* of episodic memory was already out of scope for this phase; a
future phase that decides to wire it in can revisit the hosting question (e.g.
exposing it through the existing read-only `apps/api` surface, which already
holds the `mesh_reader` role).
