"""Phase 1 of the agentic migration: the write gateway (apply_effects).

Proves each Effect routes to the invariant-preserving write path against the test
container: claims stay immutable, belief revisions are append-only, merges
re-point references, edges upsert. No LLM, no mocks beyond the seeded board.
"""
from __future__ import annotations

from datetime import UTC, datetime

from mesh_db.beliefs import create_belief, get_belief_by_id
from mesh_db.claims import create_claim, get_claim_by_id
from mesh_db.connection import MeshConnection
from mesh_db.effects import apply_effects
from mesh_db.entities import create_entity, get_entity_by_id
from mesh_db.relationships import find_relationship
from mesh_db.revisions import list_revisions
from mesh_db.sources import create_source
from mesh_models.belief import Belief
from mesh_models.claim import Claim, ClaimStatus
from mesh_models.effect import (
    AddRelationshipEvidenceEffect,
    CreateBeliefEffect,
    CreateClaimEffect,
    CreateEntityEffect,
    MergeEntitiesEffect,
    ReviseBeliefEffect,
    SupersedeClaimEffect,
)
from mesh_models.entity import Entity, EntityType
from mesh_models.source import Source, SourceType

_NOW = datetime(2026, 6, 13, tzinfo=UTC)


def _entity(conn: MeshConnection, name: str) -> Entity:
    return create_entity(conn, Entity(canonical_name=name, type=EntityType.model))


def _source(conn: MeshConnection, tag: str) -> Source:
    return create_source(
        conn,
        Source(
            type=SourceType.arxiv,
            url=f"https://example.com/{tag}",
            published_at=_NOW,
            raw_content_hash=f"hash-{tag}",
        ),
    )


def _claim(conn: MeshConnection, entity_id: str, source_id: str) -> Claim:
    return create_claim(
        conn,
        Claim(
            predicate="has_capability",
            subject_entity_id=entity_id,
            object={"capability": "reasoning"},
            source_id=source_id,
            extracted_at=_NOW,
            extracted_by_agent="claim_extractor",
            raw_excerpt="…",
        ),
    )


def test_create_claim_effect_inserts(tmp_db: MeshConnection) -> None:
    ent = _entity(tmp_db, "NewNet")
    src = _source(tmp_db, "p1")
    claim = Claim(
        predicate="has_capability",
        subject_entity_id=ent.id,
        object={"capability": "vision"},
        source_id=src.id,
        extracted_at=_NOW,
        extracted_by_agent="extract-source",
        raw_excerpt="…",
    )
    report = apply_effects(
        tmp_db, [CreateClaimEffect(field_id="ai-robotics", claim=claim)]
    )
    assert report.claims_created == 1
    assert get_claim_by_id(tmp_db, claim.id) is not None


def test_revise_belief_is_append_only(tmp_db: MeshConnection) -> None:
    ent = _entity(tmp_db, "ReviseNet")
    src = _source(tmp_db, "p2")
    claim = _claim(tmp_db, ent.id, src.id)
    belief = create_belief(
        tmp_db,
        Belief(
            topic="revisenet-cap",
            statement="ReviseNet is decent",
            supporting_claim_ids=[claim.id],
            confidence=0.5,
            is_currently_held=True,
        ),
    )
    report = apply_effects(
        tmp_db,
        [
            ReviseBeliefEffect(
                belief_id=belief.id,
                new_statement="ReviseNet is strong",
                new_confidence=0.8,
                revised_by_agent="synthesize-belief",
                rationale="new corroborating evidence",
                trigger_claim_ids=[claim.id],
            )
        ],
    )
    assert report.beliefs_revised == 1

    head = get_belief_by_id(tmp_db, belief.id)
    assert head is not None
    assert head.statement == "ReviseNet is strong"
    assert head.confidence == 0.8
    assert head.revision_count == belief.revision_count + 1

    revs = list_revisions(tmp_db, belief_id=belief.id)
    assert len(revs) == 1
    # Append-only: the revision captured the prior head verbatim.
    assert revs[0].previous_statement == "ReviseNet is decent"
    assert revs[0].new_statement == "ReviseNet is strong"
    assert revs[0].revised_by_agent == "synthesize-belief"


def test_supersede_claim_is_the_only_claim_mutation(tmp_db: MeshConnection) -> None:
    ent = _entity(tmp_db, "OldNet")
    src = _source(tmp_db, "p3")
    claim = _claim(tmp_db, ent.id, src.id)
    apply_effects(tmp_db, [SupersedeClaimEffect(claim_id=claim.id)])
    refreshed = get_claim_by_id(tmp_db, claim.id)
    assert refreshed is not None
    assert refreshed.status == ClaimStatus.superseded


def test_merge_entities_effect_repoints_claims(tmp_db: MeshConnection) -> None:
    canonical = _entity(tmp_db, "CanonNet")
    dup = _entity(tmp_db, "CanonNet (dup)")
    src = _source(tmp_db, "p4")
    claim = _claim(tmp_db, dup.id, src.id)

    report = apply_effects(
        tmp_db, [MergeEntitiesEffect(canonical_id=canonical.id, duplicate_id=dup.id)]
    )
    assert report.entities_merged == 1
    assert get_entity_by_id(tmp_db, dup.id) is None
    # The claim now points at the canonical entity (content untouched otherwise).
    moved = get_claim_by_id(tmp_db, claim.id)
    assert moved is not None
    assert moved.subject_entity_id == canonical.id


def test_add_relationship_evidence_effect_upserts_edge(tmp_db: MeshConnection) -> None:
    a = _entity(tmp_db, "A-Net")
    b = _entity(tmp_db, "B-Net")
    src = _source(tmp_db, "p5")
    claim = _claim(tmp_db, a.id, src.id)
    report = apply_effects(
        tmp_db,
        [
            AddRelationshipEvidenceEffect(
                field_id="ai-robotics",
                from_entity_id=a.id,
                to_entity_id=b.id,
                type="outperforms",
                claim_id=claim.id,
                confidence=0.7,
            )
        ],
    )
    assert report.relationship_edges == 1
    edge = find_relationship(tmp_db, a.id, b.id, "outperforms")
    assert edge is not None
    assert claim.id in edge.evidence_claim_ids


def test_best_effort_records_errors_without_aborting(tmp_db: MeshConnection) -> None:
    ent = _entity(tmp_db, "GoodNet")
    src = _source(tmp_db, "p6")
    good = Claim(
        predicate="has_capability",
        subject_entity_id=ent.id,
        object={"capability": "x"},
        source_id=src.id,
        extracted_at=_NOW,
        extracted_by_agent="extract-source",
        raw_excerpt="…",
    )
    report = apply_effects(
        tmp_db,
        [
            ReviseBeliefEffect(  # bad: belief does not exist
                belief_id="missing",
                new_statement="…",
                new_confidence=0.5,
                revised_by_agent="x",
                rationale="…",
            ),
            CreateClaimEffect(field_id="ai-robotics", claim=good),  # good: still applies
        ],
    )
    assert len(report.errors) == 1
    assert report.claims_created == 1
    assert get_claim_by_id(tmp_db, good.id) is not None


def test_create_entity_effect_inserts_with_embedding(tmp_db: MeshConnection) -> None:
    eid = "11111111-1111-1111-1111-111111111111"
    report = apply_effects(
        tmp_db,
        [
            CreateEntityEffect(
                field_id="ai-robotics",
                entity=Entity(id=eid, canonical_name="NewModel-9B", type=EntityType.model),
                name_embedding=[0.0] * 383 + [1.0],
            )
        ],
    )
    assert report.entities_created == 1
    assert get_entity_by_id(tmp_db, eid) is not None


def test_confidence_fn_overrides_the_skill_prior_on_create(tmp_db: MeshConnection) -> None:
    # The gateway recomputes a created belief's confidence from the injected fn,
    # overriding the prior the effect carried (Phase 14d evidence-derived score).
    bid = "22222222-2222-2222-2222-222222222222"
    report = apply_effects(
        tmp_db,
        [
            CreateBeliefEffect(
                field_id="ai-robotics",
                belief=Belief(
                    id=bid, topic="sota:thing", statement="X is SOTA", confidence=0.5
                ),
            )
        ],
        confidence_fn=lambda _conn, _bid: 0.87,
    )
    assert report.beliefs_created == 1
    stored = get_belief_by_id(tmp_db, bid)
    assert stored is not None
    assert abs(stored.confidence - 0.87) < 1e-9
