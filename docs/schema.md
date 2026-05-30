# Schema Reference

## Core principle: claims are immutable, beliefs are mutable

A **claim** is a historical record of what a source asserted at a point in time. Once inserted, its content fields (predicate, subject, object, source, raw_excerpt) never change. This is non-negotiable: modifying a claim would silently destroy provenance. If new evidence supersedes a claim, you create a new claim and mark the old one `superseded`.

A **belief** is the system's current synthesized view on a topic. It is explicitly mutable and carries a `revision_count` so you can always see how much it has evolved. Every revision is recorded in `BeliefRevision` (append-only), so the full audit trail is always available.

## Provenance is mandatory

- Every `Claim` must reference a `Source`. No orphan claims.
- Every `Belief` carries lists of supporting and contradicting `Claim` IDs.
- Every `Relationship` carries `evidence_claim_ids`.
- `Investigation.resolution_belief_id` closes the loop from question to answer.

---

## Entities

Represents a named thing in the AI/robotics research domain.

| Field | Type | Notes |
|-------|------|-------|
| id | UUID string | Primary key |
| canonical_name | str | The preferred name |
| aliases | list[str] | Postgres `text[]` array |
| type | EntityType enum | model, paper, benchmark, method, person, lab, repo, concept |
| attributes | JSON dict | Flexible key-value metadata |
| created_at | timestamptz | When first seen |
| last_seen_at | timestamptz | Updated on each re-encounter |

`name_embedding vector(384)` (pgvector) column exists for future entity resolution; unpopulated today.

## Sources

Where a claim came from.

| Field | Type | Notes |
|-------|------|-------|
| id | UUID string | |
| type | SourceType enum | arxiv, hn_post, hn_comment, github, twitter, blog, leaderboard |
| url | str | Canonical URL |
| author | str? | Optional |
| published_at | timestamptz | When the source was published |
| fetched_at | timestamptz | When we retrieved it |
| raw_content_hash | str | SHA-256 of raw content, for dedup |
| reliability_prior | float 0–1 | Bayesian prior on source quality (default 0.5) |

## Claims (IMMUTABLE content)

What a source asserted. Content fields are write-once.

| Field | Type | Notes |
|-------|------|-------|
| id | UUID string | |
| predicate | str | e.g. "has_parameter_count", "achieves_score_on" |
| subject_entity_id | FK → Entity | The thing being described |
| object | JSON | Flexible value (e.g. `{"value": "175B"}`) |
| source_id | FK → Source | Where this came from |
| extracted_at | timestamptz | |
| extracted_by_agent | str | Which agent created this |
| raw_excerpt | str | Verbatim text that supports the claim |
| status | ClaimStatus | active, superseded, retracted, disputed — MUTABLE |
| confidence | float 0–1 | System's current confidence — MUTABLE |
| superseded_by_claim_id | FK → Claim? | Points to replacement claim |

The only allowed update path is `update_claim_status()`. No general-purpose update function exists.

## Beliefs (MUTABLE)

The synthesized current view on a topic.

| Field | Type | Notes |
|-------|------|-------|
| id | UUID string | |
| topic | str | Broad category (e.g. "llm-scaling") |
| statement | str | A human-readable declarative sentence |
| supporting_claim_ids | list[str] | Claims that back this belief |
| contradicting_claim_ids | list[str] | Claims that challenge it |
| confidence | float 0–1 | |
| last_revised_at | timestamptz | |
| revision_count | int | How many times it has been revised |
| is_currently_held | bool | False when retracted |

## BeliefRevisions (APPEND-ONLY audit log)

Every time a belief changes, one row is added here. Never updated or deleted.

| Field | Type | Notes |
|-------|------|-------|
| id | UUID string | |
| belief_id | FK → Belief | |
| previous_statement | str | Snapshot before |
| new_statement | str | Snapshot after |
| previous_confidence | float | |
| new_confidence | float | |
| trigger_claim_ids | list[str] | What caused this revision |
| revised_by_agent | str | |
| revised_at | timestamptz | |
| rationale | str | Why the belief changed |

## Relationships

Typed edges between entities.

| Field | Type | Notes |
|-------|------|-------|
| id | UUID string | |
| from_entity_id | FK → Entity | |
| to_entity_id | FK → Entity | |
| type | str | e.g. "cites", "trained_on", "competes_with" |
| evidence_claim_ids | list[str] | Claims that support this relationship |
| confidence | float 0–1 | |

## Investigations

Pending questions the system is trying to answer.

| Field | Type | Notes |
|-------|------|-------|
| id | UUID string | |
| question | str | Natural language question |
| related_entity_ids | list[str] | |
| status | InvestigationStatus | open, active, resolved, abandoned |
| priority | float 0–1 | |
| created_at | timestamptz | |
| resolved_at | timestamptz? | |
| resolution_belief_id | FK → Belief? | The belief that answers the question |
| assigned_scout_agents | list[str] | |

---

## Example flow: a new arxiv paper appears

1. **Source created**: a row is inserted into `sources` with `type=arxiv`, the paper URL, published_at, and a hash of the raw content.
2. **Entity upserted**: the paper itself is inserted into `entities` with `type=paper`. Authors may be inserted as `type=person` entities. Referenced models (e.g. "GPT-4") are upserted as `type=model` entities.
3. **Claims extracted**: for each factual assertion in the paper (e.g. "achieves 92% on MMLU"), a `claims` row is inserted linking the subject entity, the source, and the raw excerpt. These rows are immutable from this point forward.
4. **Relationships recorded**: if the paper cites another paper, a `relationships` row is inserted with `type=cites`.
5. **Beliefs updated**: the synthesizer agent checks existing beliefs related to the claim's topic. If the new claim supports or contradicts an existing belief, that belief's statement and confidence are updated, and a `belief_revisions` row is appended recording the delta.
6. **Investigations resolved**: if an open investigation was asking the question that this paper answers, its status is set to `resolved` and `resolution_belief_id` is pointed at the updated belief.
