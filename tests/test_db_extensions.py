from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest
from mesh_db.beliefs import count_beliefs, create_belief, list_beliefs
from mesh_db.claims import (
    count_claims,
    create_claim,
    get_claims_by_ids,
    list_claims,
)
from mesh_db.connection import MeshConnection
from mesh_db.entities import (
    count_entities,
    create_entity,
    get_entities_by_ids,
    list_entities,
)
from mesh_db.revisions import create_revision, list_revisions
from mesh_db.sources import (
    count_sources,
    create_source,
    get_sources_by_ids,
    list_sources,
)
from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.entity import Entity, EntityType
from mesh_models.revision import BeliefRevision
from mesh_models.source import Source, SourceType


def _seed_entities(conn: MeshConnection, n: int) -> list[Entity]:
    out: list[Entity] = []
    for i in range(n):
        e = Entity(canonical_name=f"Model-{i:02d}", type=EntityType.model)
        create_entity(conn, e)
        out.append(e)
    return out


def _seed_source(conn: MeshConnection, suffix: str = "x") -> Source:
    s = Source(
        type=SourceType.arxiv,
        url=f"https://arxiv.org/abs/{suffix}",
        published_at=datetime.now(UTC),
        raw_content_hash=f"hash-{suffix}",
    )
    create_source(conn, s)
    return s


def test_list_entities_offset_paginates(tmp_db: MeshConnection) -> None:
    _seed_entities(tmp_db, 5)
    page1 = list_entities(tmp_db, limit=2, offset=0)
    page2 = list_entities(tmp_db, limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert {e.id for e in page1}.isdisjoint({e.id for e in page2})


def test_list_entities_limit_capped(tmp_db: MeshConnection) -> None:
    _seed_entities(tmp_db, 3)
    result = list_entities(tmp_db, limit=99999)
    assert len(result) == 3  # capped, doesn't error


def test_list_entities_q_substring(tmp_db: MeshConnection) -> None:
    create_entity(tmp_db, Entity(canonical_name="Llama-3", type=EntityType.model))
    create_entity(tmp_db, Entity(canonical_name="GPT-4", type=EntityType.model))
    create_entity(tmp_db, Entity(canonical_name="ImageNet", type=EntityType.benchmark))
    matched = list_entities(tmp_db, q="ll")  # case-insensitive: "Llama"
    names = {e.canonical_name for e in matched}
    assert "Llama-3" in names
    assert "GPT-4" not in names


def test_count_entities_matches_list(tmp_db: MeshConnection) -> None:
    _seed_entities(tmp_db, 7)
    assert count_entities(tmp_db) == 7
    create_entity(tmp_db, Entity(canonical_name="Paper-1", type=EntityType.paper))
    assert count_entities(tmp_db, type=EntityType.model) == 7
    assert count_entities(tmp_db, type=EntityType.paper) == 1


def test_get_entities_by_ids(tmp_db: MeshConnection) -> None:
    seeded = _seed_entities(tmp_db, 5)
    ids = [seeded[0].id, seeded[3].id]
    fetched = get_entities_by_ids(tmp_db, ids)
    assert {e.id for e in fetched} == set(ids)
    assert get_entities_by_ids(tmp_db, []) == []


def test_list_claims_offset_and_predicate(tmp_db: MeshConnection) -> None:
    entity = _seed_entities(tmp_db, 1)[0]
    source = _seed_source(tmp_db)
    for i in range(4):
        create_claim(
            tmp_db,
            Claim(
                predicate="achieves_score" if i % 2 == 0 else "has_parameter_count",
                subject_entity_id=entity.id,
                object={"v": i},
                source_id=source.id,
                extracted_by_agent="t",
                raw_excerpt=f"excerpt-{i}",
            ),
        )
    scoring = list_claims(tmp_db, predicate="achieves_score")
    assert all(c.predicate == "achieves_score" for c in scoring)
    assert len(scoring) == 2

    page1 = list_claims(tmp_db, limit=2, offset=0)
    page2 = list_claims(tmp_db, limit=2, offset=2)
    assert {c.id for c in page1}.isdisjoint({c.id for c in page2})


def test_count_claims_respects_filters(tmp_db: MeshConnection) -> None:
    entity = _seed_entities(tmp_db, 1)[0]
    source = _seed_source(tmp_db)
    for _ in range(3):
        create_claim(
            tmp_db,
            Claim(
                predicate="p",
                subject_entity_id=entity.id,
                object={},
                source_id=source.id,
                extracted_by_agent="t",
                raw_excerpt="x",
            ),
        )
    assert count_claims(tmp_db) == 3
    assert count_claims(tmp_db, source_id=source.id) == 3
    assert count_claims(tmp_db, predicate="other") == 0


def test_get_claims_by_ids(tmp_db: MeshConnection) -> None:
    entity = _seed_entities(tmp_db, 1)[0]
    source = _seed_source(tmp_db)
    claims = []
    for i in range(3):
        c = Claim(
            predicate=f"p-{i}",
            subject_entity_id=entity.id,
            object={},
            source_id=source.id,
            extracted_by_agent="t",
            raw_excerpt="x",
        )
        create_claim(tmp_db, c)
        claims.append(c)
    ids = [claims[0].id, claims[2].id]
    got = get_claims_by_ids(tmp_db, ids)
    assert {c.id for c in got} == set(ids)


def test_list_beliefs_offset_and_count(tmp_db: MeshConnection) -> None:
    for i in range(4):
        create_belief(
            tmp_db,
            Belief(
                topic=f"topic-{i}",
                statement=f"statement-{i}",
                confidence=0.5,
            ),
        )
    page1 = list_beliefs(tmp_db, limit=2, offset=0)
    page2 = list_beliefs(tmp_db, limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 2
    assert count_beliefs(tmp_db) == 4
    assert count_beliefs(tmp_db, topic="topic-1") == 1


def test_list_sources_offset_count_and_batch(tmp_db: MeshConnection) -> None:
    seeded = [_seed_source(tmp_db, suffix=str(i)) for i in range(3)]
    page1 = list_sources(tmp_db, limit=2, offset=0)
    page2 = list_sources(tmp_db, limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 1
    assert count_sources(tmp_db) == 3
    assert count_sources(tmp_db, type=SourceType.arxiv) == 3
    fetched = get_sources_by_ids(tmp_db, [seeded[0].id, seeded[2].id])
    assert {s.id for s in fetched} == {seeded[0].id, seeded[2].id}


def test_list_revisions_offset(tmp_db: MeshConnection) -> None:
    belief = Belief(topic="t", statement="initial", confidence=0.5)
    create_belief(tmp_db, belief)
    for i in range(3):
        create_revision(
            tmp_db,
            BeliefRevision(
                belief_id=belief.id,
                previous_statement="prev",
                new_statement=f"new-{i}",
                previous_confidence=0.5,
                new_confidence=0.6,
                trigger_claim_ids=[],
                revised_by_agent="sota",
                rationale="r",
            ),
        )
    page1 = list_revisions(tmp_db, belief_id=belief.id, limit=2, offset=0)
    page2 = list_revisions(tmp_db, belief_id=belief.id, limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 1


def test_read_only_connection_blocks_writes(_pg: str, tmp_db: MeshConnection) -> None:
    """The read-only role (mesh_reader) is SELECT-only — the Postgres equivalent
    of DuckDB's read-only file mode, now enforced by grants."""
    create_entity(tmp_db, Entity(canonical_name="seed", type=EntityType.model))

    reader_dsn = _pg.replace("test:test@", "mesh_reader:mesh_reader@")
    with psycopg.connect(reader_dsn, autocommit=True) as ro:
        ro.execute("SET search_path TO knowledge, agents, runtime, catalog, public")
        # Reads work.
        assert ro.execute("SELECT count(*) FROM entities").fetchone() == (1,)
        # Writes are refused at the DB level.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            ro.execute(
                "INSERT INTO entities (id, canonical_name, type, created_at, last_seen_at)"
                " VALUES ('blocked', 'blocked', 'model', now(), now())"
            )
