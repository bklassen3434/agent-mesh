from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mesh_db.beliefs import create_belief
from mesh_db.claims import create_claim
from mesh_db.connection import get_connection
from mesh_db.entities import create_entity
from mesh_db.migrations import apply_migrations
from mesh_db.pipeline_runs import PipelineRun, create_pipeline_run
from mesh_db.revisions import create_revision
from mesh_db.sources import create_source
from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.entity import Entity, EntityType
from mesh_models.revision import BeliefRevision
from mesh_models.source import Source, SourceType


@pytest.fixture
def empty_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A migrated but empty DB; MESH_DB_PATH points to it for the test."""
    db_path = tmp_path / "api.db"
    monkeypatch.setenv("MESH_DB_PATH", str(db_path))
    conn = get_connection(read_only=False)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return db_path


@pytest.fixture
def empty_client(empty_db_path: Path) -> Iterator[TestClient]:
    from mesh_api.main import create_app

    app = create_app()
    with TestClient(app) as client:
        yield client


@pytest.fixture
def seeded_db_path(empty_db_path: Path) -> Path:
    """A migrated DB pre-populated with a small interconnected fixture set."""
    conn = get_connection(read_only=False)
    try:
        # Two entities — one model, one benchmark
        gpt = Entity(canonical_name="GPT-4", type=EntityType.model, aliases=["gpt4"])
        imagenet = Entity(canonical_name="ImageNet", type=EntityType.benchmark)
        create_entity(conn, gpt)
        create_entity(conn, imagenet)

        # Two sources
        src_a = Source(
            type=SourceType.arxiv,
            url="https://arxiv.org/abs/aaa",
            published_at=datetime(2025, 1, 1, tzinfo=UTC),
            raw_content_hash="hash-aaa",
            reliability_prior=0.8,
        )
        src_b = Source(
            type=SourceType.arxiv,
            url="https://arxiv.org/abs/bbb",
            published_at=datetime(2025, 2, 1, tzinfo=UTC),
            raw_content_hash="hash-bbb",
            reliability_prior=0.7,
        )
        create_source(conn, src_a)
        create_source(conn, src_b)

        # Three claims — two support, one contradicts
        claim_support_1 = Claim(
            predicate="achieves_score",
            subject_entity_id=gpt.id,
            object={"benchmark": "ImageNet", "score": 0.91},
            source_id=src_a.id,
            extracted_by_agent="claim-extractor",
            raw_excerpt="GPT-4 achieves 91% top-1 on ImageNet.",
            confidence=0.9,
        )
        claim_support_2 = Claim(
            predicate="achieves_score",
            subject_entity_id=gpt.id,
            object={"benchmark": "ImageNet", "score": 0.93},
            source_id=src_b.id,
            extracted_by_agent="claim-extractor",
            raw_excerpt="Re-evaluation: GPT-4 reaches 93%.",
            confidence=0.85,
        )
        claim_contradict = Claim(
            predicate="achieves_score",
            subject_entity_id=gpt.id,
            object={"benchmark": "ImageNet", "score": 0.70},
            source_id=src_b.id,
            extracted_by_agent="claim-extractor",
            raw_excerpt="A reproducibility report finds only 70%.",
            confidence=0.6,
        )
        create_claim(conn, claim_support_1)
        create_claim(conn, claim_support_2)
        create_claim(conn, claim_contradict)

        # One belief with one revision
        belief = Belief(
            topic="GPT-4 ImageNet SOTA",
            statement="GPT-4 achieves 91% on ImageNet.",
            supporting_claim_ids=[claim_support_1.id, claim_support_2.id],
            contradicting_claim_ids=[claim_contradict.id],
            confidence=0.85,
            revision_count=1,
        )
        create_belief(conn, belief)

        create_revision(
            conn,
            BeliefRevision(
                belief_id=belief.id,
                previous_statement="GPT-4 achieves 91% on ImageNet.",
                new_statement="GPT-4 achieves 93% on ImageNet.",
                previous_confidence=0.75,
                new_confidence=0.85,
                trigger_claim_ids=[claim_support_2.id],
                revised_by_agent="sota-tracker",
                rationale="New higher score reported in arxiv.org/abs/bbb.",
            ),
        )

        # One pipeline run
        create_pipeline_run(
            conn,
            PipelineRun(
                started_at=datetime.now(UTC) - timedelta(hours=1),
                finished_at=datetime.now(UTC),
                papers_scouted=12,
                sources_inserted=2,
                claims_inserted=3,
                entities_created=2,
                beliefs_created=1,
                beliefs_revised=1,
                avg_extraction_latency_ms=842,
            ),
        )
    finally:
        conn.close()
    return empty_db_path


@pytest.fixture
def client(seeded_db_path: Path) -> Iterator[TestClient]:
    from mesh_api.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c
