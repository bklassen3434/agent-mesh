"""Phase 13 — semantic entity resolution tests.

Grows across sub-phases: 13a blocking, 13b match + merge, 13d live path.
Uses the ``tmp_db`` writer connection from conftest (pgvector testcontainer);
embeddings are constructed deterministically here so CI never downloads a model.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mesh_agents.entity_resolution import (
    EntityForMatch,
    EntityMatchDecision,
    ResolutionConfig,
    adjudicate_same_entity,
    build_adjudication_batch_items,
    classify_pair,
)
from mesh_db.claims import create_claim, get_claim_by_id
from mesh_db.connection import MeshConnection
from mesh_db.entities import (
    choose_canonical,
    create_entity,
    find_candidate_duplicates,
    get_entity_by_id,
    merge_entities,
    set_entity_embedding,
)
from mesh_db.relationships import create_relationship, list_relationships
from mesh_db.sources import create_source
from mesh_llm import EMBED_DIM
from mesh_llm.client import LLMResponseError
from mesh_models.claim import Claim
from mesh_models.entity import Entity, EntityType
from mesh_models.relationship import Relationship
from mesh_models.source import Source, SourceType


def _unit(idx: int, dim: int = EMBED_DIM) -> list[float]:
    """A unit basis vector: distinct indices are orthogonal (cosine distance 1),
    same index is identical (cosine distance 0). Lets tests control similarity
    exactly without a real embedder."""
    vec = [0.0] * dim
    vec[idx % dim] = 1.0
    return vec


def _make_entity(conn: MeshConnection, name: str, etype: EntityType, idx: int) -> str:
    ent = Entity(canonical_name=name, type=etype)
    create_entity(conn, ent)
    set_entity_embedding(conn, ent.id, _unit(idx))
    return ent.id


# --------------------------------------------------------------------------
# 13a — blocking
# --------------------------------------------------------------------------


def test_blocking_orders_by_similarity_and_excludes_self(tmp_db: MeshConnection) -> None:
    query_id = _make_entity(tmp_db, "Mamba", EntityType.model, idx=0)
    near_id = _make_entity(tmp_db, "Mamba-2", EntityType.model, idx=0)  # identical
    far_id = _make_entity(tmp_db, "Transformer", EntityType.model, idx=5)  # orthogonal

    results = find_candidate_duplicates(
        tmp_db, _unit(0), entity_type=EntityType.model, exclude_id=query_id, k=10
    )
    ids = [r[0] for r in results]

    assert query_id not in ids  # self excluded
    assert ids[0] == near_id  # nearest first
    assert far_id in ids
    assert ids.index(near_id) < ids.index(far_id)
    # distance of the identical-vector candidate is ~0, the orthogonal one ~1
    near_dist = next(r[3] for r in results if r[0] == near_id)
    far_dist = next(r[3] for r in results if r[0] == far_id)
    assert near_dist < 0.01
    assert far_dist > 0.9


def test_blocking_respects_type_filter(tmp_db: MeshConnection) -> None:
    _make_entity(tmp_db, "GPT-4 model", EntityType.model, idx=0)
    bench_id = _make_entity(tmp_db, "GPT-4 benchmark", EntityType.benchmark, idx=0)

    model_hits = find_candidate_duplicates(tmp_db, _unit(0), entity_type=EntityType.model)
    assert bench_id not in [r[0] for r in model_hits]

    bench_hits = find_candidate_duplicates(
        tmp_db, _unit(0), entity_type=EntityType.benchmark
    )
    assert bench_id in [r[0] for r in bench_hits]


def test_blocking_skips_entities_without_embedding(tmp_db: MeshConnection) -> None:
    embedded = _make_entity(tmp_db, "Embedded", EntityType.model, idx=0)
    bare = Entity(canonical_name="Bare", type=EntityType.model)
    create_entity(tmp_db, bare)  # no embedding set

    hits = find_candidate_duplicates(tmp_db, _unit(0), entity_type=EntityType.model)
    ids = [r[0] for r in hits]
    assert embedded in ids
    assert bare.id not in ids


def test_set_entity_embedding_roundtrip(tmp_db: MeshConnection) -> None:
    ent = Entity(canonical_name="Roundtrip", type=EntityType.concept)
    create_entity(tmp_db, ent)
    set_entity_embedding(tmp_db, ent.id, _unit(3))
    # Entity model read path does not expose the embedding; verify via blocking.
    hits = find_candidate_duplicates(tmp_db, _unit(3), entity_type=EntityType.concept)
    assert ent.id in [r[0] for r in hits]
    assert get_entity_by_id(tmp_db, ent.id) is not None


# --------------------------------------------------------------------------
# 13b — match bands + adjudication
# --------------------------------------------------------------------------


class _FakeLLM:
    """Minimal LLMClient stub for adjudication tests."""

    model = "fake"

    def __init__(self, decision: EntityMatchDecision | Exception) -> None:
        self._decision = decision

    def complete_with_latency(self, **kwargs: Any) -> tuple[Any, int]:
        if isinstance(self._decision, Exception):
            raise self._decision
        return self._decision, 0


def test_classify_pair_bands() -> None:
    cfg = ResolutionConfig(high=0.9, low=0.8)
    assert classify_pair(0.95, cfg) == "merge"
    assert classify_pair(0.90, cfg) == "merge"  # boundary inclusive
    assert classify_pair(0.85, cfg) == "adjudicate"
    assert classify_pair(0.80, cfg) == "reject"  # boundary inclusive
    assert classify_pair(0.5, cfg) == "reject"


def test_adjudicate_defaults_to_no_merge_on_parse_failure() -> None:
    llm = _FakeLLM(LLMResponseError("bad json"))
    a = EntityForMatch("Mamba", "model")
    b = EntityForMatch("Mamba-2", "model")
    decision = adjudicate_same_entity(llm, a, b)  # type: ignore[arg-type]
    assert decision.same_entity is False


def test_adjudicate_returns_llm_decision() -> None:
    llm = _FakeLLM(EntityMatchDecision(same_entity=True, reason="same SSM"))
    a = EntityForMatch("Mamba", "model")
    b = EntityForMatch("Mamba (SSM)", "model")
    decision = adjudicate_same_entity(llm, a, b)  # type: ignore[arg-type]
    assert decision.same_entity is True
    assert "SSM" in decision.reason


def test_build_adjudication_batch_items_carries_custom_ids() -> None:
    pairs = [
        ("p1", EntityForMatch("A", "model"), EntityForMatch("B", "model")),
        ("p2", EntityForMatch("C", "benchmark"), EntityForMatch("D", "benchmark")),
    ]
    items = build_adjudication_batch_items(pairs)
    assert [i.custom_id for i in items] == ["p1", "p2"]
    assert all(i.system and i.user for i in items)


# --------------------------------------------------------------------------
# 13b — merge
# --------------------------------------------------------------------------


def _source(conn: MeshConnection) -> str:
    src = Source(
        type=SourceType.arxiv,
        url="https://arxiv.org/abs/test",
        published_at=datetime.now(UTC),
        raw_content_hash="hash-test",
    )
    create_source(conn, src)
    return src.id


def _claim(conn: MeshConnection, entity_id: str, source_id: str, predicate: str) -> Claim:
    claim = Claim(
        predicate=predicate,
        subject_entity_id=entity_id,
        object={"value": predicate},
        source_id=source_id,
        extracted_by_agent="test",
        raw_excerpt=f"excerpt for {predicate}",
        confidence=0.7,
    )
    create_claim(conn, claim)
    return claim


def test_choose_canonical_prefers_most_claimed(tmp_db: MeshConnection) -> None:
    src = _source(tmp_db)
    a = Entity(canonical_name="A", type=EntityType.model)
    b = Entity(canonical_name="B", type=EntityType.model)
    create_entity(tmp_db, a)
    create_entity(tmp_db, b)
    _claim(tmp_db, b.id, src, "p1")
    _claim(tmp_db, b.id, src, "p2")  # B has more claims
    _claim(tmp_db, a.id, src, "p3")

    canonical, duplicate = choose_canonical(tmp_db, a.id, b.id)
    assert canonical == b.id
    assert duplicate == a.id


def test_merge_repoints_claims_without_changing_content(tmp_db: MeshConnection) -> None:
    src = _source(tmp_db)
    a = Entity(canonical_name="Mamba", type=EntityType.model)
    b = Entity(canonical_name="Mamba-2", type=EntityType.model, aliases=["mamba two"])
    create_entity(tmp_db, a)
    create_entity(tmp_db, b)
    claim = _claim(tmp_db, b.id, src, "achieves")
    before = get_claim_by_id(tmp_db, claim.id)
    assert before is not None

    merge_entities(tmp_db, a.id, b.id)

    after = get_claim_by_id(tmp_db, claim.id)
    assert after is not None
    # Reference re-pointed…
    assert after.subject_entity_id == a.id
    # …but content byte-identical.
    assert after.predicate == before.predicate
    assert after.object == before.object
    assert after.raw_excerpt == before.raw_excerpt
    assert after.confidence == before.confidence
    assert after.extracted_at == before.extracted_at
    assert after.source_id == before.source_id
    assert after.status == before.status

    # Duplicate gone; its name + aliases folded into canonical.
    assert get_entity_by_id(tmp_db, b.id) is None
    canonical = get_entity_by_id(tmp_db, a.id)
    assert canonical is not None
    lowered = {x.lower() for x in canonical.aliases}
    assert "mamba-2" in lowered
    assert "mamba two" in lowered
    assert "mamba" not in lowered  # canonical's own name not duplicated as alias


def test_merge_aggregates_duplicate_edges(tmp_db: MeshConnection) -> None:
    src = _source(tmp_db)
    a = Entity(canonical_name="A", type=EntityType.model)
    b = Entity(canonical_name="B", type=EntityType.model)
    c = Entity(canonical_name="C", type=EntityType.benchmark)
    for e in (a, b, c):
        create_entity(tmp_db, e)
    c1 = _claim(tmp_db, a.id, src, "p1")
    c2 = _claim(tmp_db, b.id, src, "p2")
    create_relationship(
        tmp_db,
        Relationship(
            from_entity_id=a.id, to_entity_id=c.id, type="evaluated_on",
            evidence_claim_ids=[c1.id], confidence=0.6,
        ),
    )
    create_relationship(
        tmp_db,
        Relationship(
            from_entity_id=b.id, to_entity_id=c.id, type="evaluated_on",
            evidence_claim_ids=[c2.id], confidence=0.8,
        ),
    )

    merge_entities(tmp_db, a.id, b.id)

    edges = list_relationships(tmp_db, from_entity_id=a.id)
    a_to_c = [e for e in edges if e.to_entity_id == c.id and e.type == "evaluated_on"]
    assert len(a_to_c) == 1  # aggregated, not parallel
    assert set(a_to_c[0].evidence_claim_ids) == {c1.id, c2.id}
    assert a_to_c[0].confidence == 0.8  # max
    assert list_relationships(tmp_db, from_entity_id=b.id) == []  # nothing left on dup


def test_merge_drops_self_loops(tmp_db: MeshConnection) -> None:
    a = Entity(canonical_name="A", type=EntityType.model)
    b = Entity(canonical_name="B", type=EntityType.model)
    create_entity(tmp_db, a)
    create_entity(tmp_db, b)
    create_relationship(
        tmp_db, Relationship(from_entity_id=a.id, to_entity_id=b.id, type="related")
    )

    merge_entities(tmp_db, a.id, b.id)  # A→B becomes A→A, must be dropped

    edges = list_relationships(tmp_db, from_entity_id=a.id)
    assert all(not (e.from_entity_id == a.id and e.to_entity_id == a.id) for e in edges)


def test_merge_is_noop_when_duplicate_missing(tmp_db: MeshConnection) -> None:
    a = Entity(canonical_name="A", type=EntityType.model)
    create_entity(tmp_db, a)
    merge_entities(tmp_db, a.id, "does-not-exist")  # no raise
    assert get_entity_by_id(tmp_db, a.id) is not None


def test_merge_same_id_is_noop(tmp_db: MeshConnection) -> None:
    a = Entity(canonical_name="A", type=EntityType.model)
    create_entity(tmp_db, a)
    merge_entities(tmp_db, a.id, a.id)
    assert get_entity_by_id(tmp_db, a.id) is not None
