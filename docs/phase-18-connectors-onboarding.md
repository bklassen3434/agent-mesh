# Phase 18 — Self-Serve Connectors + Field Onboarding

## Context

**Depends on Phase 17** (`docs/phase-17-field-isolation.md`), which must be
complete and tagged through `v0.17.0-phase-17c`. After Phase 17 the system hosts
multiple fully-isolated fields: each carries a `field_id` on all field-state
(knowledge, heuristics, runs, cost, de-dup, schedules), the active `FieldProfile`
drives the extractor/skeptic/personalizer prompts, and sources are dispatched
from a formalized **connector catalog** (`catalog.connectors`) + per-field
enablement (`catalog.field_connectors`) — with the **eight existing scouts as
built-in connectors** conforming to the `SourceConnector` protocol.

What Phase 17 does **not** provide: a way for a user to ingest from a source the
system doesn't already ship a scout for, and any UX to create/configure a field
without hand-writing rows. This phase delivers both.

Read before writing any code — do not guess details:

- The Phase 17 connector framework: the `SourceConnector` protocol, the
  `catalog.connectors` catalog (+ `config_schema`), `catalog.field_connectors`
  (per-field config), and how the coordinator dispatches enabled connectors
  (`packages/mesh-agents/`, `apps/pipeline/coordinator.py`)
- An existing config-driven-shaped scout for the fetch/emit pattern + the shared
  `Source`/`ScoutedPaper` shape and `SourceType` enum (`packages/mesh-models/`)
- The investigate-skill pattern (`investigate_arxiv`) for the `investigate_*`
  variant
- The CLI command patterns (`apps/cli/`: `investigations list`,
  `schedule status`, and any Phase 17 `field` reads)
- The wiki nav + data fetching + shadcn primitives + `make types`
  (`apps/wiki/src/`, `apps/api/`)
- The API write surface added in Phase 9 (`schedules` PATCH, scheduler proxy) +
  CORS, as the precedent for the new field/connector write endpoints

---

## Goal

Sources become a **catalog the user picks from and extends without code**: three
generic *config-driven* connectors (`web_search`, `rss`, `rest_json`) let a user
add a new source by filling a config form, and a documented developer path (with
a `pubmed` reference connector) covers structured sources that need code. Users
create, configure, and switch fields entirely from the CLI and the wiki — naming
a field, picking + configuring connectors from the catalog, and kicking off the
first run.

---

## Principles (do not violate)

- **Inherit all Phase 17 principles.** Engine doesn't move; field_id is a
  partition not a content axis; resolution/memory never cross fields;
  coordinator-owned writes; per-field cache-prefix stability; backward
  compatibility.
- **Config-driven connectors are data, not code.** A new `rss` or `rest_json`
  *instance* is a `field_connectors` row, not a new module/service. One service
  per config-driven *kind* serves arbitrarily many configured instances.
- **User-supplied connector configs are trusted input.** A generic REST/RSS/web
  connector fetches user-supplied URLs (an SSRF surface). Acceptable for a
  single-trust research deployment; do **not** add auth/sandboxing this phase,
  but keep fetches read-only, time-bounded, and `max_results`-capped, and
  document the trust assumption.
- **Connector failures never abort a run.** Fetch/parse failures record into
  `state["errors"]` via the standard `call_skill_node` path and the run
  continues — one bad feed never kills the pipeline.
- **Config validated at write time.** A connector instance's config is validated
  against the catalog `config_schema` when enabled, not mid-run.

---

## Scope

### 1. Generic config-driven connectors — block 18a

One service per *kind*, many instances via per-field config — the no-code path a
user adds from the catalog.

- **`web_search`** — driven by `web_seed_queries` config; web search + fetch;
  emits the shared shape (new `SourceType.web`). The universal fallback so any
  field ingests on day one. Also exposes the `investigate_*` variant
  (hypothesis-directed search) mirroring `investigate_arxiv`.
- **`rss`** — config: a feed URL (+ optional include/exclude terms); fetch
  Atom/RSS, emit per-item sources. New `SourceType.rss`.
- **`rest_json`** — a generic JSON-API connector. Config: endpoint URL, optional
  query template, and a small field-mapping (`items_path`, `title_path`,
  `text_path`, `url_path`, `published_path`). New `SourceType.rest`.
- Each registers itself in the catalog as `kind=config_driven` with its
  `config_schema`. Ship a docker service per kind (mirror an existing scout's
  Dockerfile/compose entry — no new pattern) and add to the available connector
  services.
- Respect the trusted-input + failure principles: read-only fetches, timeouts,
  per-run `max_results` cap, failures into `state["errors"]`, de-dupe by
  `raw_content_hash` + the per-field `processed_items` ledger.

**Exit:** a brand-new field with only `web_search` enabled ingests end-to-end; an
`rss` and a `rest_json` connector each ingest from a user-supplied config with no
code change; all output conforms to the shared shape and is field-scoped;
failures degrade gracefully; `ruff` + `mypy --strict` clean. Tag
`v0.18.0-phase-18a`.

### 2. Developer code-connector path + reference connector — block 18b

Prove "write one connector, the rest is data" for structured sources that can't
be expressed as generic config.

- Document the authoring path: implement a `SourceConnector`, register it in the
  catalog (`kind=builtin`/structured), ship its service. Put this in the docs
  (block 18d) and exercise it with a real connector.
- Ship **`pubmed`** (NCBI Entrez E-utilities) as the reference structured code
  connector for a non-AI field — keyword/date search → per-item sources in the
  shared shape, with an `investigate_pubmed` variant. (Swappable for SEC EDGAR if
  the target audience skews business over science — pick one; pubmed is the
  default.)

**Exit:** the `pubmed` code connector ingests for a medical field end-to-end via
the catalog/enablement path; output field-scoped + shared shape; the authoring
steps are documented and reproducible; `ruff` + `mypy --strict` clean. Tag
`v0.18.0-phase-18b`.

### 3. Field + connector onboarding — CLI + wiki — block 18c

Make "name your field, pick your sources, go" real for a user.

- **CLI** (`apps/cli/`, mirror `investigations list` / `schedule status` and the
  Phase 17 `field` reads):
  - `field list | show <slug> | create <name> [--bootstrap] | run <slug>`.
    `--bootstrap` calls the LLM once to draft
    `description`/`entity_type_hints`/suggested connector configs from just the
    field name, for human review.
  - `connectors list` (catalog), `connectors enable <field> <connector> --config
    <json>`, `connectors disable <field> <connector>`.
  - Writes go through the coordinator-writer path.
- **API**: `GET/POST /api/v1/fields`, `GET /api/v1/fields/{slug}`;
  `GET /api/v1/connectors` (catalog), `GET/PUT
  /api/v1/fields/{slug}/connectors` (per-field enablement + config; writer path;
  mirror the Phase 9 schedules write surface + CORS). Regenerate `make types`; CI
  drift check stays green.
- **Wiki**: a field switcher in the nav backed by `GET /api/v1/fields`; all
  knowledge views pass the selected field to the API. A "Create a field" flow +
  a per-field **connector picker**: browse the catalog, enable/configure built-ins,
  and add an `rss`/`rest_json`/`web_search` connector by filling its
  `config_schema` form; optional "run now" via the scheduler proxy. shadcn
  primitives only; no new visual language.

**Exit:** a user creates a field, picks connectors from the catalog (incl. adding
an RSS feed with no code), runs the pipeline, and sees that field's
beliefs/graph/memory isolated from `ai-robotics`; `--bootstrap` drafts a usable
profile; `make types` clean, no OpenAPI drift; `ruff` + `mypy --strict` clean;
Playwright covers the field switcher + connector picker. Tag
`v0.18.0-phase-18c`.

### 4. Docs — block 18d

Extend `docs/field-agnostic.md` (created in Phase 17) with: config-driven vs code
connectors, the user-supplied-URL trust assumption, the authoring steps for a new
code connector (with `pubmed` as the worked example), and the onboarding flow
(CLI + wiki). Update `CLAUDE.md`'s phase-status paragraph + env-var table (new
`SourceType`s, any connector service env). Match existing `docs/` style.

---

## Out of Scope (do not build)

- Connector sandboxing / SSRF protection / rate-limit infrastructure for
  user-supplied URLs — documented as a trust assumption, not built.
- A connector marketplace, connector versioning, or >1 structured code connector
  beyond the `pubmed` (or EDGAR) reference.
- Per-field engine tuning, cross-field comparison, auth / multi-tenant access
  control (all out in Phase 17 too).
- Any engine-logic change; any coordinator-write relaxation; any new role;
  mobile; visual redesign beyond the field switcher + connector picker.

---

## Exit Criteria

- [ ] `web_search`, `rss`, and `rest_json` config-driven connectors ingest a new
      field from user-supplied config with no code change; one service per kind,
      instances are `field_connectors` rows
- [ ] `pubmed` (or EDGAR) reference code connector ingests for a non-AI field via
      the catalog/enablement path; authoring steps documented + reproducible
- [ ] All connector output is field-scoped, conforms to the shared
      `Source`/`ScoutedPaper` shape, de-dupes via the per-field `processed_items`
      ledger, and fails gracefully into `state["errors"]`
- [ ] Connector config validated against `config_schema` on enable
- [ ] CLI `field` + `connectors` command groups work
- [ ] `GET/POST /api/v1/fields`, `GET /api/v1/connectors`,
      `GET/PUT /api/v1/fields/{slug}/connectors` added; `make types` clean, no
      OpenAPI drift
- [ ] Wiki field switcher + create-field flow + connector picker work and isolate
      views/memory per field; Playwright covers them
- [ ] `docs/field-agnostic.md` extended; `CLAUDE.md` updated
- [ ] `ruff` + `mypy --strict` clean across touched packages; existing pytest +
      Playwright unaffected
- [ ] All Phase 17 invariants still hold (isolation, coordinator-owned writes, no
      role relaxation, claims unmodified)

---

## Commit Convention

One logical commit per unit; conventional messages:

```
feat(agents): add web_search / rss / rest_json config-driven connectors
feat(agents): add pubmed reference code connector
feat(api): add fields + connectors endpoints
feat(cli): add `field` and `connectors` command groups
feat(wiki): add field switcher + create-field + connector picker
docs: extend field-agnostic.md (connectors + onboarding); update CLAUDE.md
```

Tags map to blocks: `v0.18.0-phase-18a` (config-driven connectors), `…-18b`
(code-connector path + pubmed), `…-18c` (onboarding CLI + wiki). Execute
18a → 18c in order. Lint, types, and a clean back-compat run are the bar before
each tag. Report any principle conflict before working around it.
