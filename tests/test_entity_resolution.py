"""Phase 13 — semantic entity resolution tests.

Grows across sub-phases: 13a blocking, 13b match + merge, 13d live path.
Uses the ``tmp_db`` writer connection from conftest (pgvector testcontainer);
embeddings are constructed deterministically here so CI never downloads a model.
"""
from __future__ import annotations

from mesh_db.connection import MeshConnection
from mesh_db.entities import (
    create_entity,
    find_candidate_duplicates,
    get_entity_by_id,
    set_entity_embedding,
)
from mesh_llm import EMBED_DIM
from mesh_models.entity import Entity, EntityType


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
