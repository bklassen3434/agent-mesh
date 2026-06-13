"""Phase 21a — field-scoped FTS + grounded-context assembly.

Verifies that the search helpers and ``gather_context`` retrieve only the
requested field's rows (the hard isolation guarantee), rank by relevance,
expand structurally from belief/entity anchors, and honor the char budget.
"""
from __future__ import annotations

from datetime import UTC, datetime

from mesh_db.beliefs import create_belief
from mesh_db.claims import create_claim
from mesh_db.connection import MeshConnection
from mesh_db.entities import create_entity
from mesh_db.fields import create_field, get_field_by_slug
from mesh_db.relationships import create_relationship
from mesh_db.search import (
    gather_context,
    search_beliefs,
    search_claims,
    search_entities,
)
from mesh_db.sources import create_source
from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.entity import Entity, EntityType
from mesh_models.field import DEFAULT_FIELD_ID, Field, FieldProfile
from mesh_models.relationship import Relationship
from mesh_models.source import Source, SourceType

_OTHER = "agribusiness"


def _ensure_other_field(conn: MeshConnection) -> str:
    existing = get_field_by_slug(conn, _OTHER)
    if existing is not None:
        return existing.id
    create_field(
        conn,
        Field(
            id=_OTHER,
            name="Agribusiness",
            slug=_OTHER,
            profile=FieldProfile(slug=_OTHER, name="Agribusiness", description="farming"),
        ),
    )
    return _OTHER


def _source(conn: MeshConnection, field_id: str, url: str) -> Source:
    src = Source(
        type=SourceType.arxiv,
        url=url,
        published_at=datetime.now(UTC),
        raw_content_hash=url,
    )
    create_source(conn, src, field_id=field_id)
    return src


def _entity(conn: MeshConnection, field_id: str, name: str) -> Entity:
    e = Entity(canonical_name=name, type=EntityType.model)
    create_entity(conn, e, field_id=field_id)
    return e


def _claim(
    conn: MeshConnection,
    field_id: str,
    *,
    entity_id: str,
    source_id: str,
    excerpt: str,
    predicate: str = "has_capability",
) -> Claim:
    c = Claim(
        predicate=predicate,
        subject_entity_id=entity_id,
        object={"capability": "x"},
        source_id=source_id,
        extracted_by_agent="claim_extractor",
        raw_excerpt=excerpt,
    )
    create_claim(conn, c, field_id=field_id)
    return c


def _belief(
    conn: MeshConnection,
    field_id: str,
    *,
    topic: str,
    statement: str,
    supporting: list[str] | None = None,
) -> Belief:
    b = Belief(
        topic=topic,
        statement=statement,
        supporting_claim_ids=supporting or [],
        last_revised_at=datetime.now(UTC),
    )
    create_belief(conn, b, field_id=field_id)
    return b


# ── FTS helpers ──────────────────────────────────────────────────────────────


def test_search_beliefs_ranks_and_scopes(tmp_db: MeshConnection) -> None:
    other = _ensure_other_field(tmp_db)
    _belief(
        tmp_db,
        DEFAULT_FIELD_ID,
        topic="transformers",
        statement="Transformer architectures dominate language benchmarks.",
    )
    _belief(
        tmp_db,
        DEFAULT_FIELD_ID,
        topic="robotics",
        statement="Robots learn dexterous manipulation from demonstrations.",
    )
    # Same wording in another field must never surface for an ai-robotics query.
    _belief(
        tmp_db,
        other,
        topic="transformers",
        statement="Transformer irrigation pumps dominate the farm.",
    )

    hits = search_beliefs(tmp_db, "transformer benchmarks", field_id=DEFAULT_FIELD_ID)
    assert hits, "expected at least one FTS hit"
    top = hits[0][0]
    assert "Transformer architectures" in top.statement
    # Field isolation: the agribusiness belief is invisible here.
    statements = {b.statement for b, _ in hits}
    assert "Transformer irrigation pumps dominate the farm." not in statements


def test_search_claims_scopes_by_field(tmp_db: MeshConnection) -> None:
    other = _ensure_other_field(tmp_db)
    e1 = _entity(tmp_db, DEFAULT_FIELD_ID, "GPT-X")
    s1 = _source(tmp_db, DEFAULT_FIELD_ID, "http://a")
    _claim(
        tmp_db,
        DEFAULT_FIELD_ID,
        entity_id=e1.id,
        source_id=s1.id,
        excerpt="GPT-X achieves remarkable reasoning on hard math problems.",
    )
    e2 = _entity(tmp_db, other, "Combine")
    s2 = _source(tmp_db, other, "http://b")
    _claim(
        tmp_db,
        other,
        entity_id=e2.id,
        source_id=s2.id,
        excerpt="Combine achieves remarkable reasoning about harvest timing.",
    )

    hits = search_claims(tmp_db, "reasoning math", field_id=DEFAULT_FIELD_ID)
    excerpts = {c.raw_excerpt for c, _ in hits}
    assert any("hard math problems" in x for x in excerpts)
    assert all("harvest timing" not in x for x in excerpts)


def test_search_entities_matches_aliases(tmp_db: MeshConnection) -> None:
    e = Entity(canonical_name="Gemini", aliases=["Bard"], type=EntityType.model)
    create_entity(tmp_db, e, field_id=DEFAULT_FIELD_ID)
    by_name = search_entities(tmp_db, "Gemini", field_id=DEFAULT_FIELD_ID)
    by_alias = search_entities(tmp_db, "Bard", field_id=DEFAULT_FIELD_ID)
    assert any(x.id == e.id for x, _ in by_name)
    assert any(x.id == e.id for x, _ in by_alias)


def test_empty_query_returns_nothing(tmp_db: MeshConnection) -> None:
    _belief(tmp_db, DEFAULT_FIELD_ID, topic="t", statement="anything at all")
    assert search_beliefs(tmp_db, "   ", field_id=DEFAULT_FIELD_ID) == []
    assert search_claims(tmp_db, "", field_id=DEFAULT_FIELD_ID) == []
    assert search_entities(tmp_db, "", field_id=DEFAULT_FIELD_ID) == []


# ── gather_context ───────────────────────────────────────────────────────────


def test_gather_context_assembles_pack_with_expansion(tmp_db: MeshConnection) -> None:
    e = _entity(tmp_db, DEFAULT_FIELD_ID, "Atlas")
    s = _source(tmp_db, DEFAULT_FIELD_ID, "http://atlas")
    c = _claim(
        tmp_db,
        DEFAULT_FIELD_ID,
        entity_id=e.id,
        source_id=s.id,
        excerpt="Atlas demonstrates bipedal parkour over uneven terrain.",
    )
    b = _belief(
        tmp_db,
        DEFAULT_FIELD_ID,
        topic="locomotion",
        statement="Atlas demonstrates state-of-the-art bipedal locomotion.",
        supporting=[c.id],
    )

    pack = gather_context(tmp_db, "Atlas bipedal locomotion", field_id=DEFAULT_FIELD_ID)
    assert not pack.is_empty()
    idx = pack.citation_index()
    assert b.id in idx["belief"]
    # the supporting claim is pulled in by structured expansion
    assert c.id in idx["claim"]
    assert e.id in idx["entity"]


def test_gather_context_empty_on_no_match(tmp_db: MeshConnection) -> None:
    _belief(
        tmp_db,
        DEFAULT_FIELD_ID,
        topic="vision",
        statement="Diffusion models generate photorealistic images.",
    )
    pack = gather_context(
        tmp_db, "quantum chromodynamics lattice gauge", field_id=DEFAULT_FIELD_ID
    )
    assert pack.is_empty()
    assert pack.citation_index() == {"belief": set(), "claim": set(), "entity": set()}


def test_gather_context_never_crosses_fields(tmp_db: MeshConnection) -> None:
    other = _ensure_other_field(tmp_db)
    # ai-robotics anchor
    _belief(
        tmp_db,
        DEFAULT_FIELD_ID,
        topic="agents",
        statement="Autonomous agents coordinate via message passing protocols.",
    )
    # agribusiness row sharing vocabulary
    eo = _entity(tmp_db, other, "Agents")
    so = _source(tmp_db, other, "http://farm")
    co = _claim(
        tmp_db,
        other,
        entity_id=eo.id,
        source_id=so.id,
        excerpt="Field agents coordinate harvest via radio protocols.",
    )
    bo = _belief(
        tmp_db,
        other,
        topic="agents",
        statement="Field agents coordinate harvest schedules.",
        supporting=[co.id],
    )

    pack = gather_context(tmp_db, "agents coordinate protocols", field_id=DEFAULT_FIELD_ID)
    idx = pack.citation_index()
    assert bo.id not in idx["belief"]
    assert co.id not in idx["claim"]
    assert eo.id not in idx["entity"]


def test_gather_context_budget_drops_claims(tmp_db: MeshConnection) -> None:
    e = _entity(tmp_db, DEFAULT_FIELD_ID, "ModelZoo")
    s = _source(tmp_db, DEFAULT_FIELD_ID, "http://zoo")
    for i in range(10):
        _claim(
            tmp_db,
            DEFAULT_FIELD_ID,
            entity_id=e.id,
            source_id=s.id,
            excerpt=f"ModelZoo benchmark run {i} reports strong reasoning scores overall.",
        )
    pack = gather_context(
        tmp_db, "ModelZoo benchmark reasoning", field_id=DEFAULT_FIELD_ID, budget=200
    )
    # The tiny budget keeps at least one claim but drops the rest.
    assert len(pack.claims) >= 1
    assert pack.dropped_claims >= 1


def test_gather_context_includes_relationship_hop(tmp_db: MeshConnection) -> None:
    a = _entity(tmp_db, DEFAULT_FIELD_ID, "Llama")
    b = _entity(tmp_db, DEFAULT_FIELD_ID, "Mistral")
    s = _source(tmp_db, DEFAULT_FIELD_ID, "http://rel")
    c = _claim(
        tmp_db,
        DEFAULT_FIELD_ID,
        entity_id=a.id,
        source_id=s.id,
        excerpt="Llama outperforms Mistral on reasoning benchmarks.",
        predicate="outperforms",
    )
    rel = Relationship(
        from_entity_id=a.id,
        to_entity_id=b.id,
        type="outperforms",
        evidence_claim_ids=[c.id],
    )
    create_relationship(tmp_db, rel, field_id=DEFAULT_FIELD_ID)

    pack = gather_context(tmp_db, "Llama reasoning benchmarks", field_id=DEFAULT_FIELD_ID)
    assert any(r.id == rel.id for r in pack.relationships)
    # the far endpoint entity was hydrated so the edge is renderable
    assert b.id in {e.id for e in pack.entities}
