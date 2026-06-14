# Verification skills

These are **evidence-capturing** verification skills, not "run the tests" shortcuts.
Each one snapshots real system state, runs assertions against it, and writes a
timestamped **evidence report** with an explicit PASS/FAIL verdict. The point is
to leave behind a durable, inspectable artifact that answers *"is this invariant
actually holding right now, and what did we observe?"* — something you can attach
to a PR, diff across runs, or hand to a reviewer.

This is distinct from `pytest` (which runs against an ephemeral testcontainer with
synthetic data). Verification skills run against a **live store / running service**
and record what they saw.

## Skills

**Static-invariant checks** (read-only; run anytime against the live store):

| Skill | What it verifies | Evidence |
|---|---|---|
| `/verify-invariants` | Core data-integrity invariants: claim immutability/supersession, append-only belief revisions, no dangling array-provenance refs (post-merge), claim_type↔predicate consistency. | row counts + offending-row samples |
| `/verify-field-isolation` | `field_id` is a true partition — no row references a row in another field (claim↔entity/source, relationship↔endpoints/evidence, belief↔claims, investigation↔target/related/belief refs) + orphan-field detection. | per-field counts + offending-row samples |
| `/verify-observability` | Routing (Phase 20) + discovery (Phase 22) surfaces: LLM-ledger integrity (non-negative tokens/cost, every row references a real run), valid investigation origins, discovery provenance; plus a reported tier-split of spend + discovery activity. | hard assertions + tier/origin report |
| `/verify-api` | The read API serves internally-consistent state: `/healthz`, `/stats`, graph + graph/data edges reference real nodes, pagination totals sane, belief detail resolves its claims, agent invocations resolve to their agent, the agent graph is a coordinator star. | captured JSON responses + assertions |

**Action checks** (snapshot → run a bounded action → snapshot → assert deltas; the action writes — run against a dev store):

| Skill | What it verifies | Evidence |
|---|---|---|
| `/verify-pipeline` | A bounded `mesh-ingest` cycle: before/after snapshot, delta consistency (sources→claims→entities→beliefs), recorded errors, then re-asserts invariants. | before/after counts + run row + diff |
| `/verify-skeptic` | A `mesh-skeptic`: deltas match the reported run (critique counter-claims, skeptic-attributed revisions, agent_reasoning sources), then re-asserts invariants. | before/after counts + run row + diff |
| `/verify-entity-resolution` | A `reconcile-entities` merge only shrank entities/relationships while leaving the claim set byte-identical; no self-loops or dangling investigation entity refs. | before/after counts + structural samples |
| `/verify-belief-consolidation` | A `consolidate-beliefs` pass stayed strictly append-only: no belief/revision row deleted, claims untouched, merged-away beliefs un-held with a revision, confidence in range. | before/after counts + structural samples |

### Coverage map (subsystem → skill)

| Subsystem | Verified by |
|---|---|
| Extract → resolve → synthesize pipeline | `/verify-pipeline` |
| Skeptic sweep (belief challenge / confidence) | `/verify-skeptic` |
| Semantic entity resolution (Phase 13) | `/verify-entity-resolution` |
| Belief consolidation + decay/archival (Phase 19) | `/verify-belief-consolidation` |
| Field partitioning (Phase 17) | `/verify-field-isolation` |
| Model routing (Phase 20) + autonomous discovery (Phase 22) | `/verify-observability` |
| Read API + wiki backend (incl. agent observability, Phase 23) | `/verify-api` |
| Cross-cutting data integrity | `/verify-invariants` |

Most action checks finish by re-running `/verify-invariants` (and often
`/verify-field-isolation`) on the freshly-written data — a green run that
violates an invariant is still a FAIL.

## Evidence convention

Every skill writes to:

```
.evidence/<skill-name>/<UTC-timestamp>/
  report.md      # human-readable verdict + per-assertion table
  report.json    # machine-readable: { verdict, assertions: [{name, passed, count, samples}], context }
  *.json / *.txt # raw captured state (snapshots, responses)
```

`.evidence/` is gitignored — evidence is captured at runtime, never committed.
Reference the path in your summary so the user can open it.

## Contract for these skills

1. **Capture before asserting.** Write the raw state you read to the evidence dir
   *first*, so a failed assertion still has its supporting data on disk.
2. **Assert with numbers, not vibes.** Each assertion is a query/check that yields
   a count; PASS iff the count is the expected value (usually 0). Record the count
   and a sample of offending rows on failure.
3. **Read-only by default.** Invariant/API checks never mutate. `/verify-pipeline`
   is the only one that runs the pipeline, and it does so with bounded inputs.
4. **Exit code is the verdict.** Helper scripts exit non-zero on any FAIL so the
   skill can be wired into CI later.
5. **Report the path.** End by telling the user the evidence directory and the
   one-line verdict.
