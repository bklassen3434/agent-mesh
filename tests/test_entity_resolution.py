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


# --------------------------------------------------------------------------
# 13c — reconciliation
# --------------------------------------------------------------------------


def _blend(i: int, j: int, cos_target: float) -> list[float]:
    """A unit vector whose cosine with _unit(i) is exactly ``cos_target`` (i!=j
    orthogonal). Lets tests place a pair in any match band."""
    import math

    vec = [0.0] * EMBED_DIM
    vec[i] = cos_target
    vec[j] = math.sqrt(1.0 - cos_target * cos_target)
    return vec


class _VecEmbedder:
    """Deterministic embedder driven by an explicit name→vector map (keyed by
    canonical name; entity type is ignored when matching)."""

    def __init__(self, by_name: dict[str, list[float]]) -> None:
        self._by_name = by_name

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            name = text.rsplit(" (", 1)[0]  # strip the " (type)" suffix
            out.append(self._by_name.get(name, _unit(0)))
        return out


def _seed(conn: MeshConnection, name: str, etype: EntityType) -> Entity:
    ent = Entity(canonical_name=name, type=etype)
    create_entity(conn, ent)
    return ent


def test_reconcile_auto_merges_high_band_clusters(tmp_db: MeshConnection) -> None:
    from mesh_agents.reconcile import reconcile_entities

    src = _source(tmp_db)
    mamba = _seed(tmp_db, "Mamba", EntityType.model)
    mamba2 = _seed(tmp_db, "Mamba-2", EntityType.model)
    transformer = _seed(tmp_db, "Transformer", EntityType.model)
    _claim(tmp_db, mamba.id, src, "p1")  # Mamba most-claimed → canonical
    _claim(tmp_db, mamba.id, src, "p2")
    _claim(tmp_db, mamba2.id, src, "p3")

    embedder = _VecEmbedder({"Mamba": _unit(0), "Mamba-2": _unit(0), "Transformer": _unit(5)})
    report = reconcile_entities(tmp_db, embedder, llm=None, dry_run=False)

    assert report.entities_before == 3
    assert report.entities_after == 2
    assert report.merges == 1
    assert report.auto_merges >= 1
    # Mamba survives (most-claimed); Mamba-2 folded in as an alias.
    survivor = get_entity_by_id(tmp_db, mamba.id)
    assert survivor is not None
    assert "mamba-2" in {a.lower() for a in survivor.aliases}
    assert get_entity_by_id(tmp_db, mamba2.id) is None
    assert get_entity_by_id(tmp_db, transformer.id) is not None


def test_reconcile_is_idempotent(tmp_db: MeshConnection) -> None:
    from mesh_agents.reconcile import reconcile_entities

    _seed(tmp_db, "Mamba", EntityType.model)
    _seed(tmp_db, "Mamba-2", EntityType.model)
    embedder = _VecEmbedder({"Mamba": _unit(0), "Mamba-2": _unit(0)})

    reconcile_entities(tmp_db, embedder, llm=None, dry_run=False)
    second = reconcile_entities(tmp_db, embedder, llm=None, dry_run=False)
    assert second.merges == 0
    assert second.entities_before == second.entities_after == 1


def test_reconcile_dry_run_makes_no_changes(tmp_db: MeshConnection) -> None:
    from mesh_agents.reconcile import reconcile_entities

    a = _seed(tmp_db, "Mamba", EntityType.model)
    b = _seed(tmp_db, "Mamba-2", EntityType.model)
    embedder = _VecEmbedder({"Mamba": _unit(0), "Mamba-2": _unit(0)})

    report = reconcile_entities(tmp_db, embedder, llm=None, dry_run=True)
    assert report.merges == 1
    assert report.entities_after == 1  # projected
    # …but nothing actually merged.
    assert get_entity_by_id(tmp_db, a.id) is not None
    assert get_entity_by_id(tmp_db, b.id) is not None


def test_reconcile_does_not_merge_across_types(tmp_db: MeshConnection) -> None:
    from mesh_agents.reconcile import reconcile_entities

    model = _seed(tmp_db, "GPT-4", EntityType.model)
    bench = _seed(tmp_db, "GPT-4", EntityType.benchmark)
    # Identical vectors, but blocking is type-filtered so they never pair.
    embedder = _VecEmbedder({"GPT-4": _unit(0)})
    report = reconcile_entities(tmp_db, embedder, llm=None, dry_run=False)
    assert report.merges == 0
    assert get_entity_by_id(tmp_db, model.id) is not None
    assert get_entity_by_id(tmp_db, bench.id) is not None


def test_reconcile_middle_band_uses_llm(tmp_db: MeshConnection) -> None:
    from mesh_agents.reconcile import reconcile_entities

    a = _seed(tmp_db, "GPT-4", EntityType.model)
    b = _seed(tmp_db, "GPT-four", EntityType.model)
    # cos ~0.85 → middle band (default high=0.93, low=0.80).
    embedder = _VecEmbedder({"GPT-4": _unit(0), "GPT-four": _blend(0, 1, 0.85)})

    # LLM says same → merged; adjudication counted.
    yes_llm = _FakeLLM(EntityMatchDecision(same_entity=True, reason="same"))
    report = reconcile_entities(tmp_db, embedder, llm=yes_llm, dry_run=False)
    assert report.adjudications == 1
    assert report.merges == 1
    assert (get_entity_by_id(tmp_db, a.id) is None) ^ (
        get_entity_by_id(tmp_db, b.id) is None
    )  # exactly one survives


def test_reconcile_middle_band_llm_no_does_not_merge(tmp_db: MeshConnection) -> None:
    from mesh_agents.reconcile import reconcile_entities

    a = _seed(tmp_db, "GPT-4", EntityType.model)
    b = _seed(tmp_db, "Gopher", EntityType.model)
    embedder = _VecEmbedder({"GPT-4": _unit(0), "Gopher": _blend(0, 1, 0.85)})

    no_llm = _FakeLLM(EntityMatchDecision(same_entity=False, reason="different"))
    report = reconcile_entities(tmp_db, embedder, llm=no_llm, dry_run=False)
    assert report.adjudications == 1
    assert report.merges == 0
    assert get_entity_by_id(tmp_db, a.id) is not None
    assert get_entity_by_id(tmp_db, b.id) is not None


# --------------------------------------------------------------------------
# 13d — live path (resolve before create)
# --------------------------------------------------------------------------


class _RaisingEmbedder:
    """Fails if embed() is called — proves the alias/exact fast-path skips it."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("embedder should not be called on the fast-path")


def _count(conn: MeshConnection) -> int:
    row = conn.execute("SELECT count(*) FROM entities").fetchone()
    return int(row[0]) if row else 0


def test_live_create_new_when_no_candidate(tmp_db: MeshConnection) -> None:
    from mesh_agents.entity_resolution import resolve_entity_semantic

    embedder = _VecEmbedder({"Mamba": _unit(0)})
    info = resolve_entity_semantic(
        tmp_db, embedder, None, "Mamba", type_hint=EntityType.model
    )
    assert info.is_new is True
    assert info.entity_type == "model"
    # created with an embedding → discoverable by blocking
    hits = find_candidate_duplicates(tmp_db, _unit(0), entity_type=EntityType.model)
    assert info.entity_id in [r[0] for r in hits]


def test_live_alias_fast_path_skips_embed_and_llm(tmp_db: MeshConnection) -> None:
    from mesh_agents.entity_resolution import resolve_entity_semantic

    ent = Entity(canonical_name="Mamba", type=EntityType.model, aliases=["MMB"])
    create_entity(tmp_db, ent)

    # Variant matches an alias → resolves with no embed (RaisingEmbedder) / no LLM.
    info = resolve_entity_semantic(tmp_db, _RaisingEmbedder(), None, "mmb")
    assert info.is_new is False
    assert info.entity_id == ent.id


def test_live_high_band_attaches_and_records_alias(tmp_db: MeshConnection) -> None:
    from mesh_agents.entity_resolution import resolve_entity_semantic

    ent = Entity(canonical_name="Mamba", type=EntityType.model)
    create_entity(tmp_db, ent)
    set_entity_embedding(tmp_db, ent.id, _unit(0))

    embedder = _VecEmbedder({"Mamba SSM": _unit(0)})  # identical vector → high band
    info = resolve_entity_semantic(
        tmp_db, embedder, None, "Mamba SSM", type_hint=EntityType.model
    )
    assert info.is_new is False
    assert info.entity_id == ent.id
    refreshed = get_entity_by_id(tmp_db, ent.id)
    assert refreshed is not None
    assert "mamba ssm" in {a.lower() for a in refreshed.aliases}
    assert _count(tmp_db) == 1  # no duplicate created


def test_live_middle_band_merges_on_llm_yes(tmp_db: MeshConnection) -> None:
    from mesh_agents.entity_resolution import resolve_entity_semantic

    ent = Entity(canonical_name="Mamba", type=EntityType.model)
    create_entity(tmp_db, ent)
    set_entity_embedding(tmp_db, ent.id, _unit(0))

    embedder = _VecEmbedder({"Gamba": _blend(0, 1, 0.85)})  # middle band
    yes = _FakeLLM(EntityMatchDecision(same_entity=True, reason="same"))
    info = resolve_entity_semantic(
        tmp_db, embedder, yes, "Gamba", type_hint=EntityType.model  # type: ignore[arg-type]
    )
    assert info.is_new is False
    assert info.entity_id == ent.id


def test_live_middle_band_creates_on_llm_no_or_absent(tmp_db: MeshConnection) -> None:
    from mesh_agents.entity_resolution import resolve_entity_semantic

    ent = Entity(canonical_name="Mamba", type=EntityType.model)
    create_entity(tmp_db, ent)
    set_entity_embedding(tmp_db, ent.id, _unit(0))
    embedder = _VecEmbedder({"Gamba": _blend(0, 1, 0.85)})

    no = _FakeLLM(EntityMatchDecision(same_entity=False, reason="diff"))
    info_no = resolve_entity_semantic(
        tmp_db, embedder, no, "Gamba", type_hint=EntityType.model  # type: ignore[arg-type]
    )
    assert info_no.is_new is True  # LLM said different → new entity

    # llm absent → conservative create (no merge without adjudication).
    # Use a different off-axis (index 2) so Gomba is middle-band to Mamba but
    # not a high-band twin of the Gamba created just above.
    embedder2 = _VecEmbedder({"Gomba": _blend(0, 2, 0.85)})
    info_none = resolve_entity_semantic(
        tmp_db, embedder2, None, "Gomba", type_hint=EntityType.model
    )
    assert info_none.is_new is True


def test_live_repeat_variant_does_not_grow_entity_count(tmp_db: MeshConnection) -> None:
    from mesh_agents.entity_resolution import resolve_entity_semantic

    embedder = _VecEmbedder({"Mamba": _unit(0)})
    first = resolve_entity_semantic(
        tmp_db, embedder, None, "Mamba", type_hint=EntityType.model
    )
    assert first.is_new is True
    assert _count(tmp_db) == 1

    # Re-encountering the exact name resolves via the fast-path — no new entity.
    second = resolve_entity_semantic(
        tmp_db, _RaisingEmbedder(), None, "Mamba", type_hint=EntityType.model
    )
    assert second.is_new is False
    assert second.entity_id == first.entity_id
    assert _count(tmp_db) == 1
