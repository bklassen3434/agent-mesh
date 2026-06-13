# Field-Agnostic Core (Phase 17)

## Why

The knowledge engine — claim extraction, semantic entity resolution, belief
synthesis, confidence scoring, the curator/skeptic/personalizer fleet, the
procedural-memory store, the graph, the API, and the wiki — was already
domain-agnostic in its *logic*. What coupled the system to "AI + robotics
research" was a thin shell: arXiv-only structured ingest with AI-defaulted
scouts, three system prompts that hardcoded *"an AI/robotics research knowledge
base"*, and the absence of any notion of a **field** — every row of field-state
lived in one undifferentiated `knowledge` schema.

Phase 17 delivers a working, fully-isolated multi-field system. A first-class
**Field** scopes all field-state; the active **Field Profile** drives the
prompts; and sources come from a formalized **connector catalog** that a field
enables and configures per-field. The existing behavior survives as one seeded
field — `ai-robotics` — with all prior data backfilled into it.

## The Field + FieldProfile

A **Field** (`knowledge.fields`, `mesh_models.field.Field`) is `id`, `name`,
`slug`, an `is_active` flag, and a stored **FieldProfile** (JSON). The profile is
the prompt-driving description of a field:

```python
class FieldProfile(BaseModel):
    slug: str
    name: str
    description: str            # the domain grounding clause for the prompts
    entity_type_hints: list[str]  # how entities are named (rule-4 examples)
    extraction_examples: str   # the few-shot block, inserted verbatim
    topic_label: str           # "state of the art" / topic wording (e.g. "sota")
```

`field_id` is a **partition, never a content axis**: synthesis, confidence,
curator scoring, and the predicate/claim-type vocabulary never read it to branch
behavior. Same code, scoped data.

The seeded `ai-robotics` profile carries the legacy grounding + the existing AI
few-shot **verbatim** (the few-shot is packaged data,
`mesh_models/_ai_robotics_examples.txt`), so its built prompts are
byte-identical to the prior hardcoded strings. `init_pg` upserts the canonical
profile from Python (`mesh_db.fields.seed_default_field`) — the SQL migration
only seeds a minimal placeholder, because the few-shot text contains characters
the naive migration runner cannot carry safely.

## Scoping boundary: what is scoped vs shared-runtime

| Scoped (carries `field_id`; every read/write filters by it) | How |
|---|---|
| `entities`, `sources`, `claims`, `beliefs`, `relationships`, `investigations`, `agent_heuristic`, `pipeline_runs` | `field_id TEXT NOT NULL` FK → `fields(id)` (migration 009) |
| `belief_revisions`, `agent_heuristic_revision` | inherit scope through their head FK (no column) |
| `processed_items` (dedup ledger) | PK extended to `(field_id, source_type, external_id)` — one source ingested independently per field |
| `schedules` (in `public`) | `field_id` column — each field its own cadence |
| per-field connector config | `knowledge.field_connectors` |
| `llm_usage` | inherits field via `run_id` (join) — no column |

| Shared runtime (field-agnostic by design) | Why |
|---|---|
| the connector **catalog** (`knowledge.connectors`) | definitions are reusable across fields; only *enablement + config* is per-field |
| LangGraph checkpoints | `thread_id` is per-run; the run carries its field in state |
| the role/permission model, the migrations | infrastructure, not field-state |

Default rule for any future table: **if it holds field knowledge or
field-learned behavior, it gets `field_id`.**

## The hard invariant: resolution + memory never cross fields

A cross-field entity merge, or a heuristic/episodic memory leaking between
fields, is a **correctness bug**. So:

- **Entity resolution** (`mesh_db.entities.find_candidate_duplicates`, the
  `mesh_agents.entity_resolution` name/alias fast-path + `resolve_entity_semantic`,
  the reconciliation backfill) all take and filter by `field_id`. "Apple" (tech)
  and "Apple" (agribusiness) never block against — let alone merge with — each
  other.
- **Procedural + episodic memory** (`list_applicable_heuristics`,
  `recall_history`, the consolidation graph) filter by `field_id`. An agent
  running in field B sees only field-B heuristics and history in its prompt; the
  consolidation graph distills per field and writes heuristics scoped to the
  field whose history it came from.

These guarantees are pinned by `tests/test_field_isolation.py` (shared-name and
shared-agent tests across two fields).

## Profile-driven prompts (per-field-stable cache prefix)

The three coupled system prompts (`CLAIM_EXTRACTION_SYSTEM`, `SKEPTIC_SYSTEM`,
`PERSONALIZER_SYSTEM`) are now **builders** taking a `FieldProfile`
(`mesh_llm.prompts.build_*`): the domain clause comes from `description`, the
rule-4 entity examples from `entity_type_hints`, and the extraction few-shot from
`extraction_examples`. The **universal core** — the predicate vocabulary, object
schemas, verdict / failure-mode / section taxonomy — is sliced verbatim from the
legacy strings and never moves per field. The `ai-robotics` profile rebuilds each
prompt byte-for-byte (asserted in `tests/test_field_prompts.py`).

Agents build the prompt from the active field's profile, loaded + cached per
field by `mesh_agents.profiles.load_profile` (best-effort; degrades to the seeded
`ai-robotics` profile with **no DB read** for the default field). The coordinator
passes `field_id` in each skill payload; the agent builds the
`cache_control`-marked system prefix **once per field**. Per-item content (and
the episodic/heuristic memory blocks) stays in the *user* message, after the
prefix — so:

**Cache-prefix rule:** templating the domain into a system prompt moves the cache
prefix *per field*, but it MUST remain byte-stable *within* a field across a run.
Build the prefix from the profile once; never interpolate per-item data into it.

## Connector model: catalog + per-field config

Sources are a **catalog** configured per field (built-in connectors only this
phase):

- **`knowledge.connectors`** — the global catalog. Each row is a connector
  *definition*: `slug`, `name`, `description`, `kind` (`builtin`), and a
  `config_schema` describing the fields a field must supply (arXiv → `categories`;
  github → `topics`; …). The catalog is seeded from the Python registry
  `mesh_models.connector.BUILTIN_CONNECTORS` by `init_pg` (so the schema lives in
  one place, not the SQL literal). The seven built-ins are the existing scouts:
  arxiv, hn, github, bluesky, reddit, blog, leaderboard.
- **`knowledge.field_connectors`** — one field's *enablement + config* of a
  catalog connector (`field_id, connector_id, config jsonb, enabled`,
  unique per `(field_id, connector_id)`, coordinator-write).
  `mesh_db.connectors.enable_connector` validates the config against the
  connector's `config_schema` at write time, so bad config is rejected on enable,
  never mid-run.

The **`SourceConnector` protocol** (`mesh_agents.connector`) formalizes the
de-facto scout interface: given a per-field `config`, a `max_results` cap, and an
optional `since` window, produce source records. Each scout's A2A skill
(`scout_<slug>`) is a conforming connector and already reads its search terms
(categories / keywords / topics / …) from the dispatched payload.

The coordinator's `scout` node loads the run field's **enabled** connectors,
maps each `slug → scout_<slug>`, intersects with the discovered scout services,
and dispatches **only** those — passing each its stored config plus the run's
`max_results`/`since`. `_DEFAULT_AGENT_URLS` is merely the set of *available*
connector services; *which* run is field-driven. The legacy `--categories` flag
is now an optional per-run override of the arxiv connector (default: use the
field's config).

The `ai-robotics` field enables every built-in with config equal to today's scout
defaults, so the seeded field behaves exactly as before (asserted in
`tests/test_connectors.py`).

## Running a field

```bash
# Pipeline (defaults to the seeded ai-robotics field):
uv run mesh-pipeline --a2a
uv run mesh-pipeline --a2a --field ai-robotics

# Skeptic sweep / entity reconciliation are field-scoped too:
uv run mesh-skeptic-sweep --field ai-robotics
uv run mesh.cli reconcile-entities --field ai-robotics --apply

# The read API scopes every knowledge endpoint by ?field=<slug> (default ai-robotics):
GET /api/v1/beliefs?field=ai-robotics
```

## Phase 18

Phase 18 (`docs/phase-18-connectors-onboarding.md`) adds the self-serve layer:
**user-addable, config-driven connectors** (web_search, rss, rest_json) that
implement the same `SourceConnector` protocol, the pubmed reference connector,
and the field/connector **onboarding UX** (CLI `field`/`connectors` groups, the
wiki field switcher + connector picker).
