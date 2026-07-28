# Self-improvement loop

The mesh grades its own output and fixes itself, on the same whiteboard/tension
primitive as everything else: **accumulate evidence → cross a threshold → act →
promote only if it helped**. No fixed schedule, no human in the loop.

## The objective is accuracy + currency, nothing synthetic

The loss is what the knowledge base is *for*: are its held beliefs **true** (the
web-grounded accuracy eval, `mesh_agents.eval.accuracy`) and **current** (the
freshness eval, `mesh_agents.eval.freshness`)? The extraction-F1 eval is a cheap
inner-loop *validator*, not the objective.

## The gradient is fault attribution

An accuracy failure is a loss at the output. `mesh_agents.eval.attribution` does the
credit assignment: for each non-supported belief it walks the provenance chain
(belief → supporting claims → sources + freshness) and an LLM diagnoser attributes
the fault to **one component** — the actuator that would fix it:

| component | the fault | the actuator |
|---|---|---|
| `extraction` | a claim misreads its source excerpt | the extract-source prompt |
| `synthesis` | claims fine, the belief overstates/mis-aggregates | the synthesize-belief prompt |
| `freshness` | the source is stale and the world moved on | scout more / a dead connector |
| `coverage` | nothing sourced covers the claim | open an investigation |
| `confidence` | the KB is confident on wrong beliefs | calibration weights |

Severity = worse verdict × judge certainty. Most failures on a live KB attribute to
staleness/coverage, not prompts — so the gradient is mostly "scout / investigate",
and only sometimes "fix a prompt". *It doesn't have to be prompts.*

## Concerns accumulate; they don't trigger edits

Each attribution is an **`ImprovementConcern`** — the loop's unit of evidence, the
analog of a claim for system quality. They're **stored, append-only**
(`agents.improvement_concerns`): unlike a normal tension (derived fresh each round),
a concern persists because a belief being wrong today is evidence that lasts. A
partial-unique-open index de-dupes an already-open concern for the same
belief+component, so a re-eval doesn't double-count.

The eval only *writes concerns*. It never edits the system.

## Threshold → A/B → promote (all on the board)

- **Sense** — a cooldown-gated `evaluate_accuracy` tension (`MESH_CONTROLLER_EVAL_COOLDOWN_SEC`,
  default 3 days) routes to the `sense-accuracy` skill, which grades a sample,
  attributes faults, and emits `RecordConcernEffect`s.
- **Activate** — `improvement_tensions` counts open concerns per component; when they
  cross `MESH_IMPROVE_CONCERN_THRESHOLD` (or summed severity
  `MESH_IMPROVE_SEVERITY_THRESHOLD`), it *derives* an `improvable_component` tension —
  exactly like `thin_belief` firing when a belief's evidence is too thin.
- **Act** — the `improve-component` skill reads the accumulated concerns (the
  gradient), seeds the prompt optimizer with them (`run_improvement(..., extra_guidance=...)`),
  and runs a **held-out A/B**: hill-climb on a train split, then compare the winner
  vs the live prompt on unseen examples.
- **Promote** — only if the candidate *actually beats* the live prompt does it emit
  `InstallPromptVersionEffect` (append-only `catalog.prompt_versions`, which the
  extractor reads via `resolve_extraction_system`) + `ResolveConcernsEffect` to close
  the concerns it acted on. If the A/B loses, no effects — the controller's stall
  cooldown backs the tension off until more concerns accrue.

Every write goes through the effects gateway; the skills-never-write invariant holds.

## Actuator coverage

Today the wired auto-actuator is **extraction** (the extract-source prompt has a
frozen-dataset A/B). Concerns for the other components accumulate and are visible but
don't auto-fire yet — the gradient is general, the actuators grow over time
(synthesis eval, connector toggles, confidence-weight search).

## Manual entry point

`mesh.cli improve-extraction-prompt --apply` runs the same A/B-and-install once by
hand (dry-run without `--apply`).
