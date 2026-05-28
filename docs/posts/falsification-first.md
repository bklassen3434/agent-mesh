# Falsification-first multi-agent systems

> Draft. Phase 7d.

Most multi-agent systems treat disagreement as something to resolve. The
synthesizer picks a winner, the orchestrator routes to whichever agent
voted hardest, the conflict gets logged and moved past. The mesh does
the opposite: it has an agent whose entire job is to disagree with
existing beliefs, and that disagreement is first-class data with a
structured taxonomy of *why* it disagrees. Write-up of the design and
what it bought us.

## The agent that exists to attack

`Skeptic` is one of twelve agents in the mesh. It is the only one that
never produces new beliefs and never asserts that anything is true. Its
input is a held belief plus the supporting + contradicting claims
attached to it. Its output is a `SkepticAssessment`:

```python
class SkepticAssessment(BaseModel):
    verdict: Literal["supported", "weakened", "contradicted", "inconclusive"]
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    suggested_confidence_delta: float = Field(ge=-1.0, le=1.0)
    counter_claims: list[SkepticCounterClaim] = Field(default_factory=list)
```

The verdict is one of four words. Verdict alone isn't useful — the
mesh doesn't act on text labels. What's useful is the `counter_claims`
list, where each counter-claim is a fully-formed Claim with:

- `subject_entity_id` (must be in the in-scope entity set; the system
  prompt is strict about this)
- `predicate` (drawn from the same enum claim_extractor uses)
- `object` (predicate-shaped JSON dict)
- `raw_excerpt` (the specific evidence problem, quoted)
- `confidence`
- **`failure_mode`** (the structured taxonomy)

These counter-claims get persisted to the `claims` table. They live
alongside scout-extracted claims. The provenance graph treats them
identically — `extracted_by_agent='skeptic'` is the only distinguishing
mark. That's the load-bearing part.

## Why "verdict" is text but "failure_mode" is an enum

Phase 7 pre-work added `Claim.failure_mode` as a `StrEnum`. Seven values:

```python
class FailureMode(StrEnum):
    unsupported_extrapolation = "unsupported_extrapolation"
    cherry_picked_evidence    = "cherry_picked_evidence"
    methodological_flaw       = "methodological_flaw"
    outdated_by_newer_claim   = "outdated_by_newer_claim"
    contradicted_by_source    = "contradicted_by_source"
    definitional_ambiguity    = "definitional_ambiguity"
    other                     = "other"
```

The `rationale` field stays — it's the human-readable narrative, useful
on the wiki and in Langfuse traces. But narrative is hard to act on. If
you want to ask "which beliefs got attacked for cherry-picked evidence
in the last month" the rationale field is useless — you'd be re-parsing
English. The enum makes it a SQL query.

This is a small move and it's the kind of thing that's tempting to
defer. We didn't. The point of having Skeptic at all is to feed
downstream consumers — and they need structure, not prose.

## What downstream consumers do with the structure

Three direct consumers as of Phase 7b:

**Curator's pick selection.** When the skeptic-sweep runs, it asks
Curator which beliefs deserve a Skeptic round. Curator scores each
belief on age, supporter weakness, confidence extremity, evidence
staleness (Phase 6a), and recent contradicting activity. The
"recent contradicting activity" signal is a count of revisions in the
last 14 days where confidence went *down* — exactly the cases where
Skeptic previously weakened the belief. So a belief that just got
attacked is more likely to get attacked again. The mesh hunts for the
flinch.

**Cooldown.** The same Curator score subtracts a cooldown penalty for
beliefs the Skeptic looked at in the last 7 days. Without this, the
Skeptic would loop on the same handful of beliefs forever. With it, the
attack rotates through the population.

**Hype-vs-substance score (Phase 7b).** A DuckDB view —
`belief_hype_substance` — combines positive and negative signals into a
single 0-1 score per belief. The hype side reads two columns:

```sql
COUNT(c.id) AS skeptic_counter_claim_count,
SUM(
    CASE WHEN c.failure_mode IN (
        'methodological_flaw',
        'cherry_picked_evidence',
        'contradicted_by_source'
    ) THEN 1 ELSE 0 END
) AS severe_failure_mode_count
```

Three of the seven failure modes are "severe" — methodological issues,
selective evidence, source misquoting. Skeptic counter-claims with any
of these failure modes count double-weight against the belief's
substance. The other four modes (unsupported_extrapolation,
outdated_by_newer_claim, definitional_ambiguity, other) count once as
"there's some attack" without the severe tag.

The result: a belief that got attacked for "definitional ambiguity" lands
in a different score bucket than a belief that got attacked for
"cherry_picked_evidence" — even though both attacks reduced its
confidence by the same amount in the revision history.

## What we get from the rotation

Belief revisions in the mesh skew toward downward confidence changes,
not upward. This is what falsification-first does to the corpus: the
average belief gets less confident over time as evidence accumulates,
except for the small minority that survive multiple Skeptic rounds and
gain confidence by surviving.

This inverts the usual LLM agent shape, where confidence drifts upward
because the failure mode of an LLM is overclaim, and most agent
architectures have nothing pushing the other way. The mesh has Skeptic
pushing the other way, every sweep, weighted by failure mode.

## The investigation loop (Phase 7a)

The follow-up move was Curator-driven investigations. When Curator sees
a belief with stale evidence, thin support, or recent contradicting
activity, it emits an `InvestigationSuggestion` alongside its Skeptic
picks. The skeptic-sweep persists them as `Investigation` rows. On the
next pipeline run, the coordinator queries open investigations and
dispatches each to the scouts whose source type matches
`suggested_source_types` via A2A capability discovery.

Lifecycle: `open → in_progress → resolved | abandoned`. Resolve when
≥3 new claims arrive; abandon when 5 pipeline runs elapsed with no new
evidence.

The investigation system isn't fully populated yet — only
`investigate_arxiv` runs a real hypothesis-directed search; the other
six scouts advertise the skill but return empty (stubs). But the loop is
closed: Skeptic finds the weakness, Curator opens the investigation,
scouts go look, the verdict either survives or dies.

## What's deferred

**DSPy.** The original Phase 7 plan had DSPy replace the hand-tuned
prompts for the LLM-using agents (claim_extractor, skeptic, curator,
personalizer) using Skeptic verdicts and belief revisions as training
signal. The pre-work to extract training tuples
(`(claim, verdict, failure_mode)`) was scoped; the optimization run
wasn't. It needs a populated DB worth of training signal which the dev
laptop hasn't accumulated yet. Filed as a follow-up.

**Per-scout investigation depth.** As above, only arxiv goes deep.
GitHub investigate could search topic + keyword; blog investigate could
filter feeds by keyword; leaderboard investigate could re-fetch + match;
HN/Reddit/Bluesky could hit their respective search APIs. Each is per-
source work, none is hard.

## Where to look at it

- The Skeptic system prompt: [`packages/mesh-llm/src/mesh_llm/prompts.py`](../../packages/mesh-llm/src/mesh_llm/prompts.py)
  (`SKEPTIC_SYSTEM`).
- Counter-claim → DB row: [`apps/pipeline/src/mesh_pipeline/skeptic_sweep.py`](../../apps/pipeline/src/mesh_pipeline/skeptic_sweep.py)
  (`_counter_to_claim`).
- Curator scoring: [`packages/mesh-agents/src/mesh_agents/curator.py`](../../packages/mesh-agents/src/mesh_agents/curator.py)
  (`score_belief`).
- Hype/substance view:
  [`packages/mesh-db/migrations/015_create_derived_signal_views.sql`](../../packages/mesh-db/migrations/015_create_derived_signal_views.sql).
- The wiki surface — `/skeptic` (recent challenges), `/beliefs/[id]` (
  the `BeliefSignalsCard`), `/beliefs/[id]/timeline` (confidence over
  time, Skeptic events in destructive color).

The pattern generalizes. Any agent system where you want the answer to
"is this true?" should have an agent whose answer is "no, here's the
specific way it's wrong." Make that answer structured. Wire the structure
into the scoring layer. Watch what the corpus does over time.
