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

## Grade all beliefs, over time (the barometer)

The `sense-accuracy` skill grades against the web on a cooldown
(`MESH_CONTROLLER_EVAL_COOLDOWN_SEC`, default 3 days). Each pass grades the
`MESH_EVAL_SAMPLE_SIZE` **least-recently-graded** beliefs — a rolling cursor
(`agents.belief_grades`) so every belief is covered over successive passes, then
re-graded oldest-first. Every grade is persisted (supported ones too), giving both a
coverage record and an **accuracy time-series** — the signal a shadow experiment
reads. It emits a `RecordGradeEffect` per belief + a `RecordConcernEffect` per fault,
and edits nothing.

## Threshold → shadow A/B → promote (all on the board)

- **Activate** — `improvement_tensions` counts open concerns per component; crossing
  `MESH_IMPROVE_CONCERN_THRESHOLD` (or summed severity `MESH_IMPROVE_SEVERITY_THRESHOLD`)
  *derives* an `improvable_component` tension, like `thin_belief` on thin evidence.
- **Draft + pre-filter** — `improve-component` reads the concerns (the gradient),
  seeds the prompt optimizer with them, and runs the **frozen held-out A/B as a cheap
  pre-filter** so we only spend a live window on a candidate that at least beats the
  live prompt offline. It does **not** install — it emits an `OpenExperimentEffect`.
- **Shadow test beside prod** — while an experiment runs, a `running_experiment`
  tension routes to `advance-experiment`, which takes real already-ingested sources
  and runs **both** the live prompt (control) and the candidate (treatment) on each,
  grades both against the source, and records the two scores
  (`RecordExperimentSampleEffect`). The candidate's claims are **discarded** — nothing
  it produces enters the KB. Sampling is paced by `MESH_CONTROLLER_EXPERIMENT_COOLDOWN_SEC`.
- **Windowed decide** — once both arms have `MESH_EXPERIMENT_MIN_SAMPLE` samples,
  `advance-experiment` compares them: treatment beats control by `MESH_EXPERIMENT_MARGIN`
  → promote (`InstallPromptVersionEffect` + `ResolveConcernsEffect`); else reject.
  Either way `DecideExperimentEffect` closes the experiment.

So a change goes live only after **winning on real inputs over a window**, never on a
single frozen-set score. Every write goes through the effects gateway.

## Actuator coverage

Today the wired auto-actuator is **extraction** (its shadow arm re-runs the
extract-source prompt on live sources). Attribution already spans the whole pipeline
(scout → extraction → entity_resolution → synthesis → challenge → confidence → decay),
so concerns for the other stages accumulate and are visible; their shadow actuators
grow over time (synthesis prompt, connector toggles, confidence-weight search).

## Manual entry point

`mesh.cli improve-extraction-prompt --apply` runs the same A/B-and-install once by
hand (dry-run without `--apply`).
