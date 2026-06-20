from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from mesh_models.belief import Belief
from mesh_models.field import DEFAULT_FIELD_ID
from mesh_models.revision import BeliefRevision

from mesh_db.connection import MeshConnection
from mesh_db.revisions import create_revision

# A callable that recomputes a belief's confidence from its (just-updated)
# evidence signals. Belief merge takes one so ``mesh_db`` stays free of the
# ``mesh_agents.confidence`` dependency (the dependency flow is one-way:
# mesh-models ← mesh-db ← mesh-agents). The caller closes over the weights and
# wires ``get_belief_signals`` → ``compute_confidence``.
ConfidenceFn = Callable[[MeshConnection, str], float]


def _row_to_belief(row: tuple[Any, ...]) -> Belief:
    (
        id_, topic, statement, supporting_claim_ids, contradicting_claim_ids,
        confidence, last_revised_at, revision_count, is_currently_held,
    ) = row[:9]
    return Belief(
        id=id_,
        topic=topic,
        statement=statement,
        supporting_claim_ids=list(supporting_claim_ids) if supporting_claim_ids else [],
        contradicting_claim_ids=list(contradicting_claim_ids) if contradicting_claim_ids else [],
        confidence=float(confidence),
        last_revised_at=(
            last_revised_at if isinstance(last_revised_at, datetime)
            else datetime.fromisoformat(str(last_revised_at))
        ),
        revision_count=int(revision_count),
        is_currently_held=bool(is_currently_held),
    )


_SELECT = (
    "SELECT id, topic, statement, supporting_claim_ids, contradicting_claim_ids, "
    "confidence, last_revised_at, revision_count, is_currently_held FROM beliefs"
)


def create_belief(
    conn: MeshConnection, model: Belief, *, field_id: str = DEFAULT_FIELD_ID
) -> Belief:
    conn.execute(
        """
        INSERT INTO beliefs (id, field_id, topic, statement, supporting_claim_ids,
            contradicting_claim_ids, confidence, last_revised_at, revision_count,
            is_currently_held)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            model.id,
            field_id,
            model.topic,
            model.statement,
            model.supporting_claim_ids,
            model.contradicting_claim_ids,
            model.confidence,
            model.last_revised_at,
            model.revision_count,
            model.is_currently_held,
        ],
    )
    return model


def _vector_literal(embedding: list[float]) -> str:
    """pgvector text input format: ``[0.1,0.2,...]``. Used with a ``::vector``
    cast so we need no extra psycopg type adapter (mirrors
    ``mesh_db.entities._vector_literal``)."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def set_belief_embedding(
    conn: MeshConnection, id: str, embedding: list[float]
) -> None:
    """Populate ``statement_embedding`` for a belief (Phase 19a). Mirrors
    ``set_entity_embedding`` — the embedding is a consolidation-layer write, not a
    belief-content change (no revision is recorded for it)."""
    conn.execute(
        "UPDATE beliefs SET statement_embedding = %s::vector WHERE id = %s",
        [_vector_literal(embedding), id],
    )


def get_belief_by_id(conn: MeshConnection, id: str) -> Belief | None:
    row = conn.execute(f"{_SELECT} WHERE id = %s", [id]).fetchone()
    return _row_to_belief(row) if row else None


MAX_LIMIT = 200


def _belief_filters(
    topic: str | None, currently_held: bool | None, field_id: str | None = None
) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if field_id is not None:
        conditions.append("field_id = %s")
        params.append(field_id)
    if topic is not None:
        conditions.append("topic ILIKE %s")
        params.append(f"%{topic}%")
    if currently_held is not None:
        conditions.append("is_currently_held = %s")
        params.append(currently_held)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return where, params


def list_beliefs(
    conn: MeshConnection,
    topic: str | None = None,
    currently_held: bool | None = None,
    limit: int = 100,
    offset: int = 0,
    field_id: str | None = None,
) -> list[Belief]:
    limit = min(max(limit, 0), MAX_LIMIT)
    offset = max(offset, 0)
    where, params = _belief_filters(topic, currently_held, field_id)
    params.extend([limit, offset])
    rows = conn.execute(
        f"{_SELECT}{where} ORDER BY last_revised_at DESC LIMIT %s OFFSET %s", params
    ).fetchall()
    return [_row_to_belief(r) for r in rows]


def count_beliefs(
    conn: MeshConnection,
    topic: str | None = None,
    currently_held: bool | None = None,
    field_id: str | None = None,
) -> int:
    where, params = _belief_filters(topic, currently_held, field_id)
    row = conn.execute(f"SELECT COUNT(*) FROM beliefs{where}", params).fetchone()
    return int(row[0]) if row else 0


def find_stale_beliefs(
    conn: MeshConnection,
    threshold_days: int,
    limit: int = 100,
    field_id: str | None = None,
) -> list[Belief]:
    """Beliefs whose most recent supporting/contradicting claim is older than ``threshold_days``.

    A belief with no claims attached is treated as stale (no fresh evidence).
    Ordered by the oldest most-recent-claim first so callers (e.g. Curator)
    can prioritize the staler ones. Currently-held beliefs only — superseded
    beliefs don't need re-evaluation.
    """
    cutoff = datetime.now(UTC) - timedelta(days=threshold_days)
    limit = min(max(limit, 0), MAX_LIMIT)
    field_condition = "b.field_id = %s AND " if field_id is not None else ""
    params: list[Any] = [field_id] if field_id is not None else []
    params.extend([cutoff, limit])
    # Join via UNNEST on each claim-id array, MAX the extracted_at across both
    # to get the most recent evidence timestamp per belief. COALESCE so the
    # no-claims case sorts oldest first via a far-past sentinel.
    rows = conn.execute(
        f"""
        WITH belief_claim_links AS (
            SELECT id AS belief_id,
                   UNNEST(supporting_claim_ids) AS claim_id
            FROM beliefs WHERE is_currently_held = TRUE
            UNION ALL
            SELECT id AS belief_id,
                   UNNEST(contradicting_claim_ids) AS claim_id
            FROM beliefs WHERE is_currently_held = TRUE
        ),
        belief_evidence AS (
            SELECT b.id AS belief_id,
                   MAX(c.extracted_at) AS last_claim_at
            FROM beliefs b
            LEFT JOIN belief_claim_links bcl ON bcl.belief_id = b.id
            LEFT JOIN claims c ON c.id = bcl.claim_id
            WHERE b.is_currently_held = TRUE
            GROUP BY b.id
        )
        SELECT b.id, b.topic, b.statement, b.supporting_claim_ids,
               b.contradicting_claim_ids, b.confidence, b.last_revised_at,
               b.revision_count, b.is_currently_held
        FROM beliefs b
        JOIN belief_evidence be ON be.belief_id = b.id
        WHERE {field_condition}COALESCE(be.last_claim_at, TIMESTAMPTZ '1970-01-01') < %s
        ORDER BY be.last_claim_at ASC NULLS FIRST
        LIMIT %s
        """,
        params,
    ).fetchall()
    return [_row_to_belief(r) for r in rows]


def get_belief_signals(conn: MeshConnection, belief_id: str) -> dict[str, int]:
    """Read a belief's evidence signals from the belief_signals view (Phase 14d).

    Returns all-zero signals for a belief the view doesn't cover (e.g. not
    currently held). The view recomputes on read, so it reflects a belief's
    claim links as soon as they're written."""
    row = conn.execute(
        """
        SELECT source_type_diversity, reproduction_count,
               skeptic_counter_claim_count, severe_failure_mode_count,
               claims_last_30d
        FROM belief_signals WHERE belief_id = %s
        """,
        [belief_id],
    ).fetchone()
    if row is None:
        return {
            "source_type_diversity": 0,
            "reproduction_count": 0,
            "skeptic_counter_claim_count": 0,
            "severe_failure_mode_count": 0,
            "claims_last_30d": 0,
        }
    return {
        "source_type_diversity": int(row[0]),
        "reproduction_count": int(row[1]),
        "skeptic_counter_claim_count": int(row[2]),
        "severe_failure_mode_count": int(row[3]),
        "claims_last_30d": int(row[4]),
    }


def update_belief(
    conn: MeshConnection, id: str, **fields: Any
) -> Belief:
    allowed = {
        "statement", "supporting_claim_ids", "contradicting_claim_ids",
        "confidence", "last_revised_at", "revision_count", "is_currently_held",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        belief = get_belief_by_id(conn, id)
        if belief is None:
            raise ValueError(f"Belief {id} not found")
        return belief

    set_clauses = [f"{k} = %s" for k in updates]
    params: list[Any] = list(updates.values())
    params.append(id)
    conn.execute(
        f"UPDATE beliefs SET {', '.join(set_clauses)} WHERE id = %s", params
    )
    belief = get_belief_by_id(conn, id)
    if belief is None:
        raise ValueError(f"Belief {id} not found after update")
    return belief


# ---------------------------------------------------------------------------
# Belief consolidation (Phase 19b) — block → choose → merge, the world-model
# analog of entity merge (mesh_db.entities). Unlike entity merge it is strictly
# APPEND-ONLY: a merged-away belief is marked is_currently_held = false and keeps
# all its revisions; NO row is ever deleted (migration 011 adds no DELETE grant).
# ---------------------------------------------------------------------------


# Coarse belief families. Synthesis only ever writes `sota:*` (score) and
# `capability:*` beliefs (relational claim_types become relationship edges, not
# beliefs), so these two families partition the held corpus. Blocking restricts
# candidates to the same family: a score belief and a capability belief assert
# different kinds of proposition and must never merge, even if their embeddings
# drift close. `other` is a forward-compatible catch-all.
FAMILY_SCORE = "score"
FAMILY_CAPABILITY = "capability"
FAMILY_OTHER = "other"


def belief_family(topic: str) -> str:
    """Map a belief topic to its coarse family (see the family constants)."""
    if topic.startswith("sota:"):
        return FAMILY_SCORE
    if topic.startswith("capability:"):
        return FAMILY_CAPABILITY
    return FAMILY_OTHER


def _family_condition(family: str | None) -> str:
    """SQL fragment restricting ``topic`` to one family (empty when None).

    The ``%`` in the LIKE patterns is doubled (``%%``) so psycopg's ``%s``
    placeholder parser passes it through as a literal wildcard."""
    if family == FAMILY_SCORE:
        return " AND topic LIKE 'sota:%%'"
    if family == FAMILY_CAPABILITY:
        return " AND topic LIKE 'capability:%%'"
    if family == FAMILY_OTHER:
        return " AND topic NOT LIKE 'sota:%%' AND topic NOT LIKE 'capability:%%'"
    return ""


def find_duplicate_belief_pairs(
    conn: MeshConnection,
    *,
    field_id: str = DEFAULT_FIELD_ID,
    min_similarity: float = 0.85,
    limit: int = 50,
) -> list[tuple[str, str, str, str, float]]:
    """Pairs of currently-held, same-field, **same-family** beliefs whose
    statements embed close enough to be likely duplicates (controller tension:
    ``redundant_beliefs``). The belief analog of
    ``entities.find_duplicate_candidate_pairs`` — one pgvector self-join, each
    unordered pair once, most-similar first. Returns
    ``(id_a, topic_a, id_b, topic_b, similarity)``.

    Family is enforced in Python (``belief_family``) rather than SQL so the single
    source of truth for the score/capability split stays the helper above: a score
    belief and a capability belief never pair even if their embeddings drift close.
    Beliefs with no ``statement_embedding`` are skipped (the consolidation backfill
    populates them)."""
    distance_max = 1.0 - float(min_similarity)
    rows = conn.execute(
        """
        SELECT b1.id, b1.topic, b2.id, b2.topic,
               1 - (b1.statement_embedding <=> b2.statement_embedding) AS similarity
        FROM beliefs b1
        JOIN beliefs b2
          ON b2.field_id = b1.field_id
         AND b2.id > b1.id
         AND b2.is_currently_held = TRUE
        WHERE b1.field_id = %s
          AND b1.is_currently_held = TRUE
          AND b1.statement_embedding IS NOT NULL
          AND b2.statement_embedding IS NOT NULL
          AND (b1.statement_embedding <=> b2.statement_embedding) <= %s
        ORDER BY similarity DESC
        LIMIT %s
        """,
        [field_id, distance_max, max(int(limit), 0) * 4],
    ).fetchall()
    pairs: list[tuple[str, str, str, str, float]] = []
    for r in rows:
        topic_a, topic_b = str(r[1]), str(r[3])
        if belief_family(topic_a) != belief_family(topic_b):
            continue  # cross-family near-neighbours are not duplicates
        pairs.append((str(r[0]), topic_a, str(r[2]), topic_b, float(r[4])))
        if len(pairs) >= limit:
            break
    return pairs


def find_candidate_duplicate_beliefs(
    conn: MeshConnection,
    embedding: list[float],
    *,
    k: int = 10,
    exclude_id: str | None = None,
    field_id: str = DEFAULT_FIELD_ID,
    family: str | None = None,
) -> list[tuple[str, str, str, float]]:
    """Blocking query: the ``k`` nearest **currently-held**, **same-field**
    beliefs by cosine distance. Mirrors ``entities.find_candidate_duplicates``.

    Returns ``(id, topic, statement, distance)`` ordered nearest-first. Cosine
    distance (``<=>``) is in ``[0, 2]``; similarity is ``1 - distance`` for the
    normalised vectors the embedder produces (the resolver bands on similarity).
    Always scoped to ``field_id`` — belief consolidation never crosses fields
    (Phase 17). Beliefs have no ``type``; ``family`` optionally restricts to one
    coarse family (score / capability) so the two never cross-merge."""
    conditions = [
        "statement_embedding IS NOT NULL",
        "is_currently_held = TRUE",
        "field_id = %s",
    ]
    params: list[Any] = [_vector_literal(embedding), field_id]
    if exclude_id is not None:
        conditions.append("id <> %s")
        params.append(exclude_id)
    where = " AND ".join(conditions) + _family_condition(family)
    params.append(max(int(k), 0))
    rows = conn.execute(
        f"""
        SELECT id, topic, statement, statement_embedding <=> %s::vector AS distance
        FROM beliefs
        WHERE {where}
        ORDER BY distance
        LIMIT %s
        """,
        params,
    ).fetchall()
    return [(str(r[0]), str(r[1]), str(r[2]), float(r[3])) for r in rows]


def choose_canonical_belief(
    conn: MeshConnection, id_a: str, id_b: str
) -> tuple[str, str]:
    """Pick which of two beliefs is canonical. Returns ``(canonical, duplicate)``.

    Rule (documented, deterministic, mirrors ``entities.choose_canonical``'s
    keep-the-more-established posture): the belief with **more supporting claims**
    wins (it carries the most provenance); ties break to **higher
    ``revision_count``** (more-curated), then **earliest ``last_revised_at``**
    (the older, more-settled belief), then the **lexicographically smaller id**.
    """
    belief_a = get_belief_by_id(conn, id_a)
    belief_b = get_belief_by_id(conn, id_b)
    if belief_a is None or belief_b is None:
        raise ValueError(f"Cannot choose canonical: {id_a} or {id_b} not found")

    def key(belief: Belief) -> tuple[int, int, datetime, str]:
        return (
            -len(belief.supporting_claim_ids),
            -belief.revision_count,
            belief.last_revised_at,
            belief.id,
        )

    ordered = sorted([belief_a, belief_b], key=key)
    return ordered[0].id, ordered[1].id


def _union_preserve(base: list[str], extra: list[str]) -> tuple[list[str], list[str]]:
    """Union ``extra`` into ``base`` preserving order, de-duplicated. Returns
    ``(merged, newly_added)`` — ``newly_added`` are the ``extra`` ids not already
    in ``base`` (the provenance a merge revision records)."""
    seen = set(base)
    merged = list(base)
    added: list[str] = []
    for item in extra:
        if item not in seen:
            seen.add(item)
            merged.append(item)
            added.append(item)
    return merged, added


def merge_beliefs(
    conn: MeshConnection,
    canonical_id: str,
    duplicate_id: str,
    *,
    confidence_fn: ConfidenceFn | None = None,
) -> None:
    """Fold ``duplicate_id`` onto ``canonical_id`` in a single transaction
    (Phase 19b). Append-only: NO row is deleted; the duplicate is marked
    ``is_currently_held = false`` and absorbed. Coordinator/writer-owned.

    Steps:
      1. union the duplicate's supporting / contradicting claim ids onto the
         canonical (set-dedup, order-preserving) — claim *content* is never
         touched, only the id references;
      2. recompute the canonical's confidence from the enlarged evidence via
         ``confidence_fn`` (reads ``belief_signals`` after the union); left
         unchanged when no ``confidence_fn`` is supplied;
      3. re-point belief FK references (``investigations.opened_by_belief_id`` /
         ``resolution_belief_id``) from the duplicate to the canonical;
      4. append a ``BeliefRevision`` to the canonical (trigger_claim_ids = the
         newly-folded ids, ``revised_by_agent = "belief_consolidator"``,
         rationale naming the absorbed belief);
      5. mark the duplicate not-held and append its own merge revision.

    Idempotent: a no-op if the duplicate is already gone / not-held.
    """
    if canonical_id == duplicate_id:
        return
    canonical = get_belief_by_id(conn, canonical_id)
    duplicate = get_belief_by_id(conn, duplicate_id)
    if duplicate is None or not duplicate.is_currently_held:
        return  # already merged / gone
    if canonical is None:
        raise ValueError(f"Canonical belief {canonical_id} not found")

    merged_supporting, added_supporting = _union_preserve(
        canonical.supporting_claim_ids, duplicate.supporting_claim_ids
    )
    merged_contradicting, added_contradicting = _union_preserve(
        canonical.contradicting_claim_ids, duplicate.contradicting_claim_ids
    )
    folded_claim_ids = added_supporting + added_contradicting
    now = datetime.now(UTC)

    with conn.raw.transaction():
        # 1. Union claim-id references onto the canonical. Update the belief row
        #    BEFORE appending its revision: the belief_revisions → beliefs FK
        #    rejects an UPDATE of a row already referenced by a freshly-inserted
        #    revision in the same tx (same quirk the skeptic sweep documents).
        update_belief(
            conn,
            canonical_id,
            supporting_claim_ids=merged_supporting,
            contradicting_claim_ids=merged_contradicting,
            last_revised_at=now,
            revision_count=canonical.revision_count + 1,
        )
        # 2. Recompute confidence from the enlarged evidence (signals now reflect
        #    the union, visible within this transaction).
        new_confidence = canonical.confidence
        if confidence_fn is not None:
            new_confidence = confidence_fn(conn, canonical_id)
            update_belief(conn, canonical_id, confidence=new_confidence)
        # 3. Re-point belief FK references duplicate → canonical (UPDATE only).
        conn.execute(
            "UPDATE investigations SET opened_by_belief_id = %s "
            "WHERE opened_by_belief_id = %s",
            [canonical_id, duplicate_id],
        )
        conn.execute(
            "UPDATE investigations SET resolution_belief_id = %s "
            "WHERE resolution_belief_id = %s",
            [canonical_id, duplicate_id],
        )
        # 4. Append the canonical's merge revision (statement unchanged).
        create_revision(
            conn,
            BeliefRevision(
                belief_id=canonical_id,
                previous_statement=canonical.statement,
                new_statement=canonical.statement,
                previous_confidence=canonical.confidence,
                new_confidence=new_confidence,
                trigger_claim_ids=folded_claim_ids,
                revised_at=now,
                revised_by_agent="belief_consolidator",
                rationale=(
                    f"absorbed duplicate belief {duplicate_id} "
                    f"({len(folded_claim_ids)} claim ids folded)"
                ),
            ),
        )
        # 5. Mark the duplicate not-held + append its own merge revision. The
        #    duplicate row and all its revisions remain for audit.
        update_belief(
            conn,
            duplicate_id,
            is_currently_held=False,
            last_revised_at=now,
            revision_count=duplicate.revision_count + 1,
        )
        create_revision(
            conn,
            BeliefRevision(
                belief_id=duplicate_id,
                previous_statement=duplicate.statement,
                new_statement=duplicate.statement,
                previous_confidence=duplicate.confidence,
                new_confidence=duplicate.confidence,
                trigger_claim_ids=[],
                revised_at=now,
                revised_by_agent="belief_consolidator",
                rationale=f"merged into {canonical_id}",
            ),
        )
