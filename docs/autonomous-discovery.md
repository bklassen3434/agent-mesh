# Autonomous Discovery

Phase 22 turns the mesh from *reactive* follow-up (one belief at a time) into
something that looks at a **whole field** and decides, on its own, what to find
out next. It is the proactive sibling of [Investigations](investigations.md):
same `Investigation` table, same dispatch plumbing, a different trigger.

## Reactive Curator vs. proactive Discovery

| | Curator (Phase 7a) | Discovery (Phase 22) |
|---|---|---|
| Scope | one held belief at a time | the whole field's knowledge state |
| Trigger | this belief looks thin/stale/contested | a field-level gap or rising trend |
| Cadence | every controller round | the controller's `investigate-gap` rule |
| Origin stamp | `origin = "curator"` | `origin = "discovery"` |

Both paths emit `Investigation` rows and feed the **same** dispatch node. Neither
writes claims or beliefs directly — they only open investigations and gather
sources. New knowledge always flows through the normal extract → resolve →
synthesize path. **Discovery proposes evidence-gathering, never facts.**

## The mechanism

Discovery is the controller's `investigate-gap` rule (the gap tensions →
`investigate-gap` skill). Per field, in a controller round:

1. **plan** — `mesh_agents.discovery.analyze_field` (rule-based, no LLM) mines
   the field's state into ranked `GapSignal`s, then `draft_hypotheses` (one LLM
   pass) turns the top gaps into testable hypotheses, and
   `build_discovery_investigations` maps those to `Investigation` models —
   deduped against open ones and capped by `MESH_DISCOVER_MAX_NEW`. The capped
   set is opened as `origin="discovery"` investigations via the
   `OpenInvestigationEffect`.
2. **dispatch** — the `dispatch-investigation` skill works the open investigations
   via the shared `dispatch_open_investigations` (investigate → extract → resolve
   → insert-claims), capped by `MESH_DISCOVER_MAX_FETCH`. Only sources backed by a
   connector **enabled for the field** are dispatched; fetch failures are recorded
   per-skill and never abort the round.

The drafting LLM call is ledgered to `llm_usage` and traced in Langfuse like any
other controller skill.

## Gap / trend signal taxonomy

`analyze_field` is rule-based and field-scoped — it never crosses fields. Each
`GapSignal` carries the triggering metrics + a machine rationale so an opened
investigation is explainable.

| Kind | Detected from | Meaning |
|---|---|---|
| `under_evidenced_entity` | `entities.under_evidenced_entities` (≤1 claim) | the mesh barely knows this entity |
| `thin_belief` | `list_beliefs` + `belief_signals` (low source diversity, <2 supporters) | a held belief rests on weak evidence |
| `stale_belief` | `beliefs.find_stale_beliefs` | no fresh evidence in N days |
| `rising_topic` | `claims.recent_claim_counts_by_entity` (velocity) | an entity drawing a burst of recent claims |
| `missing_reciprocal_edge` | `relationships.list_relationships` | `A outperforms B` with no head-to-head from B's side |

`draft_hypotheses` is the only LLM step. It is framed by the field's
`FieldProfile` (`build_discovery_system`), proposes only what to search for,
constrains `suggested_source_types` to the field's enabled connectors, and
degrades to an empty list on any LLM failure — one bad pass never crashes the
sweep.

## Explainability & provenance

Every autonomous investigation records *why* it was opened:

- `origin = "discovery"` distinguishes it from Curator/skeptic/manual ones.
- `trigger_rationale` is the human-readable gap rationale + the LLM's reason.

```bash
uv run mesh.cli investigations list --origin discovery
```

## Real investigate handlers (Phase 22b)

The dispatch path runs **real** hypothesis-directed search where it makes sense:

- `investigate_arxiv` — free-text arxiv search (Phase 7a).
- `investigate_github` — free-text GitHub repo search keyed off the hypothesis.
- `investigate_leaderboard` — snapshots all leaderboard lanes on demand.
- `investigate_web` — Brave web search; the universal config-driven fallback
  (any field that enables the `web_search` connector can always search the open
  web for a hypothesis).

Other scouts (hn, reddit, blog, bluesky) keep advertising the skill but return
empty; config-driven `rss`/`rest_json` have no investigate (a single fixed
feed/endpoint has no hypothesis-search semantics). The dispatch is tolerant of a
connector that advertises no investigate skill — it just skips it.

## Dry-run preview from the CLI

`mesh.cli discover` is a read-only preview: it prints the gaps + the hypotheses
discovery *would* open, without writing. Acting on discovery is the controller's
job (`mesh-controller --apply`).

```bash
# Print the gaps + the hypotheses it WOULD open (no writes).
uv run mesh.cli discover --field ai-robotics
uv run mesh.cli discover --report-path /tmp/discovery.txt
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `MESH_DISCOVER_MAX_NEW` | `5` | Max `discovery`-origin investigations opened per field per round. |
| `MESH_DISCOVER_MAX_FETCH` | `10` | Max source records discovery dispatch gathers per field. |
| `MESH_DISCOVER_GAP_LIMIT` | `20` | Max gap signals `analyze_field` returns. |
| `MESH_LLM_MODEL_DISCOVERY` | (routing/provider default) | Per-agent model pin for the drafting LLM. |

The drafting call uses `make_routed_llm_client(agent_name="discovery")`, so a
static `MESH_LLM_MODEL_DISCOVERY` pin always wins; otherwise tiered routing
(Phase 20) keeps the frequent, cheap gap analysis on the cheap tier.

## Field isolation

Gap/trend analysis, hypothesis drafting, and dispatch all scope to one
`field_id`. A field's discovery never reads or seeds another. The controller runs
per field, so each field's gaps are analysed and dispatched independently.

## Out of scope

- Writing claims/beliefs directly from discovery (knowledge flows only through
  extract → resolve → synthesize).
- Auto-enabling connectors or editing connector config.
- Open-ended web agents / tool-use loops beyond the connector `investigate`
  contract.
- Cross-field discovery or transfer; a learned gap-prioritization policy.
