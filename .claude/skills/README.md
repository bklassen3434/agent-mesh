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

| Skill | What it verifies | Evidence |
|---|---|---|
| `/verify-invariants` | Core data-integrity invariants on the live knowledge store: claim immutability/supersession, append-only belief revisions, no dangling array-provenance refs (post-merge), claim_type↔predicate consistency. | row counts + offending-row samples |
| `/verify-pipeline` | A bounded pipeline cycle does what it claims: before/after state snapshot, delta consistency (sources→claims→entities→beliefs), recorded errors, then re-asserts invariants. | before/after counts + run row + diff |
| `/verify-api` | The read API serves internally-consistent state: `/healthz`, `/stats`, graph edges reference real nodes, pagination totals are sane, belief detail resolves its claims. | captured JSON responses + assertions |

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
