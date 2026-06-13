"""Field-scoped full-text search + grounded-context assembly (Phase 21a).

Two layers, both reader-safe (only SELECTs, no writes):

1. ``search_beliefs`` / ``search_claims`` / ``search_entities`` — Postgres
   full-text search over the corpus, each filtered by ``field_id`` and ranked
   by ``ts_rank``. The ``to_tsvector`` expressions here are byte-identical to
   the GIN expression indexes in migration 012 so the planner uses them.

2. ``gather_context`` — the retrieval orchestrator for the knowledge chatbot.
   It runs FTS over the three tables for a question, does a bounded structured
   expansion (supporting/contradicting claims + belief signals for the top
   beliefs, one hop of relationships + recent claims for the top entities), and
   returns a citation-keyed, token-budgeted :class:`ContextPack`.

Field isolation is absolute: every entry-point query filters by ``field_id``,
and the structured expansion only ever traverses from same-field anchors, so a
question scoped to field B can never surface field-A rows.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.entity import Entity
from mesh_models.field import DEFAULT_FIELD_ID
from mesh_models.relationship import Relationship

from mesh_db.beliefs import _SELECT as _BELIEF_SELECT
from mesh_db.beliefs import _row_to_belief, get_belief_signals
from mesh_db.claims import _SELECT as _CLAIM_SELECT
from mesh_db.claims import _row_to_claim, get_claims_by_ids, list_claims
from mesh_db.connection import MeshConnection
from mesh_db.entities import _row_to_entity, get_entities_by_ids
from mesh_db.relationships import list_relationships

logger = logging.getLogger(__name__)

# tsvector expressions — MUST stay byte-identical to migration 012's GIN indexes
# so the planner can use them. The query string is bound once via a derived
# ``websearch_to_tsquery`` column (``q``) and referenced in both the ``@@`` test
# and ``ts_rank``.
_BELIEF_TSV = "to_tsvector('english', topic || ' ' || statement)"
_CLAIM_TSV = "to_tsvector('english', raw_excerpt)"
_ENTITY_TSV = (
    "to_tsvector('english', "
    "canonical_name || ' ' || knowledge.immutable_alias_text(aliases))"
)

_ENTITY_COLS = (
    "id, canonical_name, aliases, type, attributes, created_at, last_seen_at"
)

# OR-semantics tsquery. websearch_to_tsquery ANDs terms by default, which is
# too strict for retrieval — a short entity name like "Atlas" would never match
# the question "Atlas bipedal locomotion". Rewriting the AND operators to OR
# turns the query into high-recall matching; ts_rank + LIMIT then order and cap
# the result, and the relevance floor drops zero-rank noise. Phrase operators
# (``<->``) are left intact.
_OR_TSQUERY = "replace(websearch_to_tsquery('english', %s)::text, '&', '|')::tsquery"


def _with_rank(select: str, tsv: str) -> str:
    """Append a trailing ``ts_rank(...) AS rank`` column to a ``SELECT … FROM``.

    The row mappers read ``row[:N]``, so the rank lands at ``row[-1]`` without
    disturbing the existing column positions. The query string ``%s`` is bound
    to a derived ``q`` column (one bind, used by both ``@@`` and ``ts_rank``).
    """
    head, _, table = select.partition(" FROM ")
    return (
        f"{head}, ts_rank({tsv}, q) AS rank "
        f"FROM {table} CROSS JOIN (SELECT {_OR_TSQUERY} AS q) AS _q"
    )

# Direct-FTS caps (per table) before structured expansion.
_BELIEF_LIMIT = 8
_CLAIM_LIMIT = 12
_ENTITY_LIMIT = 8
# How many top anchors to expand structurally.
_EXPAND_BELIEFS = 5
_EXPAND_ENTITIES = 5
_REL_LIMIT = 12
_ENTITY_CLAIM_LIMIT = 4


# ── FTS helpers ──────────────────────────────────────────────────────────────


def search_beliefs(
    conn: MeshConnection,
    query: str,
    *,
    field_id: str = DEFAULT_FIELD_ID,
    limit: int = _BELIEF_LIMIT,
) -> list[tuple[Belief, float]]:
    """Currently-held beliefs in ``field_id`` matching ``query``, ranked."""
    if not query.strip():
        return []
    rows = conn.execute(
        f"""
        {_with_rank(_BELIEF_SELECT, _BELIEF_TSV)}
        WHERE field_id = %s AND is_currently_held = TRUE AND {_BELIEF_TSV} @@ q
        ORDER BY rank DESC, last_revised_at DESC
        LIMIT %s
        """,
        [query, field_id, max(int(limit), 0)],
    ).fetchall()
    return [(_row_to_belief(r), float(r[-1])) for r in rows]


def search_claims(
    conn: MeshConnection,
    query: str,
    *,
    field_id: str = DEFAULT_FIELD_ID,
    limit: int = _CLAIM_LIMIT,
) -> list[tuple[Claim, float]]:
    """Active claims in ``field_id`` whose excerpt matches ``query``, ranked."""
    if not query.strip():
        return []
    rows = conn.execute(
        f"""
        {_with_rank(_CLAIM_SELECT, _CLAIM_TSV)}
        WHERE field_id = %s AND status = 'active' AND {_CLAIM_TSV} @@ q
        ORDER BY rank DESC, extracted_at DESC
        LIMIT %s
        """,
        [query, field_id, max(int(limit), 0)],
    ).fetchall()
    return [(_row_to_claim(r), float(r[-1])) for r in rows]


def search_entities(
    conn: MeshConnection,
    query: str,
    *,
    field_id: str = DEFAULT_FIELD_ID,
    limit: int = _ENTITY_LIMIT,
) -> list[tuple[Entity, float]]:
    """Entities in ``field_id`` whose name/aliases match ``query``, ranked."""
    if not query.strip():
        return []
    rows = conn.execute(
        f"""
        SELECT {_ENTITY_COLS}, ts_rank({_ENTITY_TSV}, q) AS rank
        FROM entities CROSS JOIN (SELECT {_OR_TSQUERY} AS q) AS _q
        WHERE field_id = %s AND {_ENTITY_TSV} @@ q
        ORDER BY rank DESC, created_at DESC
        LIMIT %s
        """,
        [query, field_id, max(int(limit), 0)],
    ).fetchall()
    return [(_row_to_entity(r), float(r[-1])) for r in rows]


# ── context pack ─────────────────────────────────────────────────────────────


@dataclass
class ScoredBelief:
    """A retrieved belief plus its evidence signals and FTS rank."""

    belief: Belief
    signals: dict[str, int]
    rank: float


@dataclass
class ContextPack:
    """A citation-keyed, budget-bounded bundle handed to the answer agent.

    Citation ids are the rows' own ids: ``beliefs[*].belief.id``,
    ``claims[*].id``, ``entities[*].id``. The agent cites by ``(kind, id)`` and
    drops any id not present in :meth:`citation_index`.
    """

    question: str
    field_id: str
    beliefs: list[ScoredBelief] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    dropped_claims: int = 0

    def is_empty(self) -> bool:
        """True when nothing relevant was retrieved (→ ``uncovered``)."""
        return not (self.beliefs or self.claims or self.entities)

    def citation_index(self) -> dict[str, set[str]]:
        """Allowed citation ids by kind — the agent validates against this."""
        return {
            "belief": {sb.belief.id for sb in self.beliefs},
            "claim": {c.id for c in self.claims},
            "entity": {e.id for e in self.entities},
        }


def _context_budget(budget: int | None) -> int:
    if budget is not None:
        return budget
    raw = os.environ.get("MESH_QA_CONTEXT_BUDGET")
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning("invalid_MESH_QA_CONTEXT_BUDGET", extra={"value": raw})
    return 12000


def _relevance_floor() -> float:
    raw = os.environ.get("MESH_QA_RELEVANCE_FLOOR")
    if raw:
        try:
            return float(raw)
        except ValueError:
            logger.warning("invalid_MESH_QA_RELEVANCE_FLOOR", extra={"value": raw})
    return 1e-6


def _claim_cost(c: Claim) -> int:
    return len(c.raw_excerpt) + len(c.predicate) + 80


def gather_context(
    conn: MeshConnection,
    question: str,
    *,
    field_id: str = DEFAULT_FIELD_ID,
    budget: int | None = None,
) -> ContextPack:
    """Assemble a field-scoped, citation-keyed, budget-bounded context pack.

    Deterministic and explainable: FTS over beliefs/claims/entities, then a
    bounded structured expansion from the top anchors. The character budget
    (``budget`` or ``MESH_QA_CONTEXT_BUDGET``, default 12000) trims the claim
    set — the bulkiest part — keeping the highest-ranked claims; the number
    dropped is logged and recorded on the pack.
    """
    char_budget = _context_budget(budget)
    floor = _relevance_floor()
    pack = ContextPack(question=question, field_id=field_id)
    if not question.strip():
        return pack

    # 1. Direct FTS over the three tables.
    belief_hits = [
        (b, r)
        for b, r in search_beliefs(conn, question, field_id=field_id, limit=_BELIEF_LIMIT)
        if r >= floor
    ]
    entity_hits = [
        (e, r)
        for e, r in search_entities(conn, question, field_id=field_id, limit=_ENTITY_LIMIT)
        if r >= floor
    ]
    claim_hits = [
        (c, r)
        for c, r in search_claims(conn, question, field_id=field_id, limit=_CLAIM_LIMIT)
        if r >= floor
    ]

    pack.beliefs = [
        ScoredBelief(belief=b, signals=get_belief_signals(conn, b.id), rank=r)
        for b, r in belief_hits
    ]
    pack.entities = [e for e, _ in entity_hits]

    # ordered claim accumulation: direct FTS hits first (by rank), then
    # structured-expansion claims. Dedup by id, never crossing fields (every
    # source is a same-field anchor).
    claims_by_id: dict[str, Claim] = {}
    claim_order: list[str] = []

    def _add_claim(c: Claim) -> None:
        if c.id not in claims_by_id:
            claims_by_id[c.id] = c
            claim_order.append(c.id)

    for c, _ in claim_hits:
        _add_claim(c)

    # 2a. Expand the top beliefs → their supporting/contradicting claims.
    expand_ids: list[str] = []
    for sb in pack.beliefs[:_EXPAND_BELIEFS]:
        expand_ids.extend(sb.belief.supporting_claim_ids)
        expand_ids.extend(sb.belief.contradicting_claim_ids)
    seen = set(claims_by_id)
    fresh = [cid for cid in dict.fromkeys(expand_ids) if cid not in seen]
    for c in get_claims_by_ids(conn, fresh):
        _add_claim(c)

    # 2b. Expand the top entities → one hop of relationships + recent claims.
    rels: dict[str, Relationship] = {}
    for e in pack.entities[:_EXPAND_ENTITIES]:
        for rel in list_relationships(
            conn, from_entity_id=e.id, field_id=field_id, limit=_REL_LIMIT
        ):
            rels[rel.id] = rel
        for rel in list_relationships(
            conn, to_entity_id=e.id, field_id=field_id, limit=_REL_LIMIT
        ):
            rels[rel.id] = rel
        for c in list_claims(
            conn, entity_id=e.id, field_id=field_id, limit=_ENTITY_CLAIM_LIMIT
        ):
            _add_claim(c)
    pack.relationships = list(rels.values())[:_REL_LIMIT]

    # 3. Budget-trim the claim set (the bulk), keeping FTS-ranked order.
    kept: list[Claim] = []
    spent = sum(len(sb.belief.topic) + len(sb.belief.statement) + 80 for sb in pack.beliefs)
    for cid in claim_order:
        c = claims_by_id[cid]
        cost = _claim_cost(c)
        if kept and spent + cost > char_budget:
            continue
        kept.append(c)
        spent += cost
    dropped = len(claim_order) - len(kept)
    if dropped:
        logger.info(
            "qa_context_budget_trim",
            extra={"field_id": field_id, "dropped_claims": dropped, "budget": char_budget},
        )
    pack.claims = kept
    pack.dropped_claims = dropped

    # Keep only relationships whose endpoints we can name from the entity set —
    # plus the anchors themselves are already present; fetch any missing
    # endpoint entities so the agent can render readable edges, field-scoped.
    _hydrate_relationship_endpoints(conn, pack)
    return pack


def _hydrate_relationship_endpoints(conn: MeshConnection, pack: ContextPack) -> None:
    """Add any relationship-endpoint entities not already in the pack.

    Endpoints come from same-field relationships, so the fetched entities are
    same-field by construction. Citeable like any other retrieved entity.
    """
    have = {e.id for e in pack.entities}
    endpoint_ids = {
        eid
        for rel in pack.relationships
        for eid in (rel.from_entity_id, rel.to_entity_id)
        if eid not in have
    }
    if not endpoint_ids:
        return
    extra = get_entities_by_ids(conn, sorted(endpoint_ids))
    pack.entities.extend(extra)
