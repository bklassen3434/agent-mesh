from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from mesh_db.beliefs import create_belief
from mesh_db.claims import create_claim
from mesh_db.connection import get_connection
from mesh_db.entities import create_entity
from mesh_db.revisions import create_revision
from mesh_db.sources import create_source
from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.entity import Entity, EntityType
from mesh_models.revision import BeliefRevision
from mesh_models.source import Source, SourceType


def _seed_skeptic_activity(db_path: Path) -> tuple[str, str]:
    """Add a belief + a skeptic-triggered revision on top of the empty DB.

    Returns (belief_id, skeptic_revision_id).
    """
    conn = get_connection(read_only=False)
    try:
        entity = Entity(canonical_name="ScopeModel", type=EntityType.model)
        create_entity(conn, entity)

        seed_source = Source(
            type=SourceType.arxiv,
            url="https://arxiv.org/abs/2024.10001",
            published_at=datetime(2024, 10, 1, tzinfo=UTC),
            raw_content_hash="hash-seed",
        )
        create_source(conn, seed_source)

        original_claim = Claim(
            predicate="achieves_score",
            subject_entity_id=entity.id,
            object={"benchmark": "Bench", "score": 90.0},
            source_id=seed_source.id,
            extracted_by_agent="claim_extractor",
            raw_excerpt="ScopeModel scores 90 on Bench.",
            confidence=0.9,
        )
        create_claim(conn, original_claim)

        belief = Belief(
            topic="sota:Bench",
            statement="ScopeModel scores 90 on Bench (as of 2024-10-01)",
            supporting_claim_ids=[original_claim.id],
            confidence=0.7,
        )
        create_belief(conn, belief)

        # Skeptic-emitted source + counter-claim + revision
        agent_source = Source(
            type=SourceType.agent_reasoning,
            url=f"agent://skeptic/belief/{belief.id}/20260520T000000Z",
            author="skeptic",
            published_at=datetime(2026, 5, 20, tzinfo=UTC),
            raw_content_hash="hash-agent",
            reliability_prior=0.4,
        )
        create_source(conn, agent_source)

        counter = Claim(
            predicate="achieves_score",
            subject_entity_id=entity.id,
            object={"benchmark": "Bench", "score": 78.0},
            source_id=agent_source.id,
            extracted_by_agent="skeptic",
            raw_excerpt="Recent supporters report only 78%.",
            confidence=0.8,
        )
        create_claim(conn, counter)

        revision = BeliefRevision(
            belief_id=belief.id,
            previous_statement=belief.statement,
            new_statement=belief.statement,
            previous_confidence=0.7,
            new_confidence=0.55,
            trigger_claim_ids=[counter.id],
            revised_by_agent="skeptic",
            rationale="Supporting claim is stale; recent re-evaluation lower.",
        )
        create_revision(conn, revision)

        # Plus an unrelated synthesis revision to confirm the filter excludes it
        synth_revision = BeliefRevision(
            belief_id=belief.id,
            previous_statement=belief.statement,
            new_statement=belief.statement,
            previous_confidence=0.55,
            new_confidence=0.55,
            trigger_claim_ids=[],
            revised_by_agent="sota_tracker",
            rationale="No-op pass.",
        )
        create_revision(conn, synth_revision)

        return belief.id, revision.id
    finally:
        conn.close()


def test_recent_skeptic_activity_empty(empty_client: TestClient) -> None:
    r = empty_client.get("/api/v1/skeptic/recent")
    assert r.status_code == 200
    assert r.json() == []


def test_recent_skeptic_activity_filters_to_skeptic_only(
    empty_client: TestClient, empty_db_path: Path
) -> None:
    belief_id, skeptic_rev_id = _seed_skeptic_activity(empty_db_path)
    r = empty_client.get("/api/v1/skeptic/recent")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    item = items[0]
    assert item["revision"]["id"] == skeptic_rev_id
    assert item["revision"]["revised_by_agent"] == "skeptic"
    assert item["belief"]["id"] == belief_id
    assert len(item["trigger_claims"]) == 1
    assert item["trigger_claims"][0]["extracted_by_agent"] == "skeptic"


def test_recent_skeptic_activity_respects_limit(
    empty_client: TestClient, empty_db_path: Path
) -> None:
    _seed_skeptic_activity(empty_db_path)
    # Only one skeptic revision was seeded; passing limit=1 should still work.
    r = empty_client.get("/api/v1/skeptic/recent?limit=1")
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_belief_revisions_endpoint(client: TestClient) -> None:
    listing = client.get("/api/v1/beliefs").json()
    bid = listing["items"][0]["id"]
    r = client.get(f"/api/v1/beliefs/{bid}/revisions")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["revision"]["new_statement"] == "GPT-4 achieves 93% on ImageNet."
    assert len(body[0]["trigger_claims"]) == 1


def test_belief_revisions_endpoint_404_for_missing_belief(client: TestClient) -> None:
    r = client.get("/api/v1/beliefs/missing/revisions")
    assert r.status_code == 404
