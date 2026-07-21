"""Phase 2 skill: ``synthesize-belief`` (claims → beliefs + graph edges).

Seeds a board, runs the skill (no LLM — the synthesizers it reuses are rule-based
or pure), and asserts it emits the right ``Effect``s without ever writing. A final
slice pushes the effects through the real gateway to prove they land.
"""
from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime
from typing import Any

import mesh_agents.skills.synthesize_belief as _synth_mod
import pytest
from mesh_agents.skill import clear_registry, load_builtin_skills
from mesh_agents.synthesis import capability_topic
from mesh_db.beliefs import create_belief, list_beliefs
from mesh_db.claims import create_claim
from mesh_db.connection import MeshConnection
from mesh_db.effects import apply_effects
from mesh_db.entities import create_entity
from mesh_db.relationships import find_relationship
from mesh_db.revisions import list_revisions
from mesh_db.sources import create_source
from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.effect import (
    AddRelationshipEvidenceEffect,
    CreateBeliefEffect,
    CreateEntityEffect,
    MarkClaimsSynthesizedEffect,
    ReviseBeliefEffect,
)
from mesh_models.entity import Entity, EntityType
from mesh_models.source import Source, SourceType
from mesh_models.tension import Tension, TensionKind

_NOW = datetime(2026, 6, 19, tzinfo=UTC)
_FIELD = "ai-robotics"


@pytest.fixture(autouse=True)
def _registry() -> Any:
    """Isolate the registry, then force the real skill's ``@register_skill`` to run.

    ``load_builtin_skills`` imports the skill module, but Python caches that import
    — once another test has cleared the registry, a plain re-import won't
    re-register. Reloading the module re-executes the decorator deterministically.
    """
    clear_registry()
    importlib.reload(_synth_mod)
    yield
    clear_registry()


class _StubEmbedder:
    """Deterministic 384-dim unit vectors (matching the ``vector(384)`` column) so
    minting an edge target populates a ``name_embedding`` without loading the real
    fastembed model."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 383 + [1.0] for _ in texts]


def _skill() -> Any:
    # Construct the (reloaded) concrete skill with a stub embedder so minting an
    # edge target doesn't load the real fastembed model. Registration itself is
    # covered by test_load_builtin_skills_registers_synthesize_belief.
    return _synth_mod.SynthesizeBeliefSkill(embedder=_StubEmbedder())


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


def _claim(
    conn: MeshConnection,
    entity_id: str,
    source_id: str,
    predicate: str,
    object: dict[str, Any],
) -> Claim:
    return create_claim(
        conn,
        Claim(
            predicate=predicate,
            subject_entity_id=entity_id,
            object=object,
            source_id=source_id,
            extracted_at=_NOW,
            extracted_by_agent="claim_extractor",
            raw_excerpt="…",
        ),
    )


def _tension(entity_id: str) -> Tension:
    return Tension(
        id=f"unsynthesized_claims:{entity_id}",
        field_id=_FIELD,
        kind=TensionKind.unsynthesized_claims,
        subject="SomeModel",
        rationale="has claims not yet reflected in any belief",
        value=0.65,
        est_cost_usd=0.05,
        handler_skill="synthesize-belief",
        target_ref={"entity_id": entity_id},
    )


def _run(skill: Any, tension: Tension, conn: MeshConnection) -> list[Any]:
    return asyncio.run(skill.run(conn, tension, budget_usd=0.05))


# ── registration ─────────────────────────────────────────────────────────────


def test_load_builtin_skills_registers_synthesize_belief() -> None:
    ids = {s.skill_id for s in load_builtin_skills()}
    assert "synthesize-belief" in ids


def test_handles_unsynthesized_claims() -> None:
    assert TensionKind.unsynthesized_claims in _skill().handles


# ── score claims → SOTA belief ───────────────────────────────────────────────


def test_score_claim_yields_create_belief_effect(tmp_db: MeshConnection) -> None:
    ent = _entity(tmp_db, "FastNet")
    src = _source(tmp_db, "s1")
    _claim(
        tmp_db,
        ent.id,
        src.id,
        "achieves_score",
        {"benchmark": "MMLU", "score": 91.2, "metric": "accuracy"},
    )

    effects = _run(_skill(), _tension(ent.id), tmp_db)

    create = [e for e in effects if isinstance(e, CreateBeliefEffect)]
    assert len(create) == 1
    assert create[0].field_id == _FIELD
    assert create[0].belief.topic == "sota:MMLU"


# ── capability claims → entity-anchored belief ───────────────────────────────


def test_capability_claim_yields_create_belief_effect(tmp_db: MeshConnection) -> None:
    ent = _entity(tmp_db, "CapNet")
    src = _source(tmp_db, "s2")
    _claim(tmp_db, ent.id, src.id, "has_capability", {"capability": "long context"})

    effects = _run(_skill(), _tension(ent.id), tmp_db)

    create = [e for e in effects if isinstance(e, CreateBeliefEffect)]
    assert len(create) == 1
    assert create[0].belief.topic == capability_topic(ent.id)


def test_existing_capability_belief_yields_revise_effect(
    tmp_db: MeshConnection,
) -> None:
    ent = _entity(tmp_db, "GrowNet")
    src = _source(tmp_db, "s3")
    first = _claim(tmp_db, ent.id, src.id, "has_capability", {"capability": "vision"})
    # A belief already reflects the first capability; a *new* claim should revise it.
    create_belief(
        tmp_db,
        Belief(
            topic=capability_topic(ent.id),
            statement="GrowNet: vision",
            supporting_claim_ids=[first.id],
            confidence=0.5,
            is_currently_held=True,
        ),
        field_id=_FIELD,
    )
    _claim(tmp_db, ent.id, src.id, "has_capability", {"capability": "planning"})

    effects = _run(_skill(), _tension(ent.id), tmp_db)

    revise = [e for e in effects if isinstance(e, ReviseBeliefEffect)]
    assert len(revise) == 1
    assert revise[0].revised_by_agent == "synthesize-belief"
    assert "planning" in revise[0].new_statement


def test_idempotent_capability_synthesis_emits_no_belief_change(
    tmp_db: MeshConnection,
) -> None:
    """A belief already in sync with the evidence → no belief revision. But the
    claim is still marked synthesized, so the entity stops re-firing its tension
    (that terminal mark is the whole point — an idempotent re-run must retire the
    claim, not churn on it forever)."""
    ent = _entity(tmp_db, "SteadyNet")
    src = _source(tmp_db, "s4")
    c = _claim(tmp_db, ent.id, src.id, "has_capability", {"capability": "reasoning"})
    create_belief(
        tmp_db,
        Belief(
            topic=capability_topic(ent.id),
            statement="SteadyNet: reasoning",
            supporting_claim_ids=[c.id],
            confidence=0.5,
            is_currently_held=True,
        ),
        field_id=_FIELD,
    )

    effects = _run(_skill(), _tension(ent.id), tmp_db)
    assert not [e for e in effects if isinstance(e, (CreateBeliefEffect, ReviseBeliefEffect))]
    marks = [e for e in effects if isinstance(e, MarkClaimsSynthesizedEffect)]
    assert len(marks) == 1
    assert marks[0].claim_ids == [c.id]


# ── relational claims → graph edge ───────────────────────────────────────────


def test_relational_claim_yields_edge_effect(tmp_db: MeshConnection) -> None:
    a = _entity(tmp_db, "ChampNet")
    b = _entity(tmp_db, "RivalNet")
    src = _source(tmp_db, "s5")
    _claim(tmp_db, a.id, src.id, "outperforms", {"compared_to": "RivalNet"})

    effects = _run(_skill(), _tension(a.id), tmp_db)

    edges = [e for e in effects if isinstance(e, AddRelationshipEvidenceEffect)]
    assert len(edges) == 1
    assert edges[0].from_entity_id == a.id
    assert edges[0].to_entity_id == b.id
    assert edges[0].type == "outperforms"
    assert edges[0].field_id == _FIELD


def test_unknown_edge_target_is_minted_then_linked(tmp_db: MeshConnection) -> None:
    """An edge to a not-yet-known target mints the target rather than skipping the
    claim — the old skip left the claim forever unsynthesized, churning its
    tension every pass. The mint effect precedes the edge that FKs it, and both
    land through the gateway."""
    a = _entity(tmp_db, "LoneNet")
    src = _source(tmp_db, "s6")
    _claim(tmp_db, a.id, src.id, "outperforms", {"compared_to": "GhostNet"})

    effects = _run(_skill(), _tension(a.id), tmp_db)

    mints = [e for e in effects if isinstance(e, CreateEntityEffect)]
    edges = [e for e in effects if isinstance(e, AddRelationshipEvidenceEffect)]
    assert len(mints) == 1
    assert mints[0].entity.canonical_name == "GhostNet"
    # Field-agnostic: a target we've never extracted is minted as the fallback
    # "concept" (no edge-kind guess), not a domain-specific type.
    assert mints[0].entity.type == "concept"
    assert mints[0].name_embedding is not None
    assert len(edges) == 1
    assert edges[0].to_entity_id == mints[0].entity.id
    # Mint precedes the edge so the gateway creates the node before FKing it.
    assert effects.index(mints[0]) < effects.index(edges[0])

    report = apply_effects(tmp_db, effects)
    assert report.entities_created == 1
    assert report.relationship_edges == 1
    assert find_relationship(tmp_db, a.id, mints[0].entity.id, "outperforms") is not None


def test_nonleader_score_claim_stops_refiring_after_synthesis(
    tmp_db: MeshConnection,
) -> None:
    """The churn regression: a score claim that isn't the leaderboard record-holder
    never enters any belief (SOTA keeps only the leader), so it used to re-fire the
    entity's tension forever. Once synthesize marks it processed, the entity drops
    out of the unsynthesized count — until a genuinely new claim arrives."""
    from mesh_db.claims import unsynthesized_claim_counts_by_entity

    ent = _entity(tmp_db, "RunnerUp")
    src = _source(tmp_db, "s-score")
    # Two scores on the same benchmark; only the higher becomes the SOTA leader.
    _claim(tmp_db, ent.id, src.id, "achieves_score", {"benchmark": "GLUE", "score": 80.0})
    _claim(tmp_db, ent.id, src.id, "achieves_score", {"benchmark": "GLUE", "score": 70.0})

    # Before synthesis the entity is on the board (2 unsynthesized score claims).
    assert dict(unsynthesized_claim_counts_by_entity(tmp_db, field_id=_FIELD)).get(ent.id) == 2

    effects = _run(_skill(), _tension(ent.id), tmp_db)
    apply_effects(tmp_db, effects)

    # After synthesis: the SOTA belief exists, and BOTH score claims are marked
    # processed — so the entity no longer appears in the count (no more churn).
    assert ent.id not in dict(unsynthesized_claim_counts_by_entity(tmp_db, field_id=_FIELD))

    # A genuinely new claim re-triggers (it arrives unmarked).
    _claim(tmp_db, ent.id, src.id, "has_capability", {"capability": "new skill"})
    assert dict(unsynthesized_claim_counts_by_entity(tmp_db, field_id=_FIELD)).get(ent.id) == 1


def test_entity_with_more_than_one_page_of_claims_is_fully_marked(
    tmp_db: MeshConnection,
) -> None:
    """An entity with more claims than list_claims' per-call MAX_LIMIT (200) must
    have ALL its claims processed and marked — a single capped read left the tail
    unread/unmarked, so the entity re-fired its tension forever (the live stall)."""
    from mesh_db.claims import unsynthesized_claim_counts_by_entity

    ent = _entity(tmp_db, "MegaBench")
    src = _source(tmp_db, "s-mega")
    # 250 evaluation claims (> the 200 page size), each to a distinct benchmark.
    for i in range(250):
        _claim(tmp_db, ent.id, src.id, "evaluated_on", {"benchmark": f"Bench-{i}"})

    assert dict(unsynthesized_claim_counts_by_entity(tmp_db, field_id=_FIELD)).get(ent.id) == 250

    effects = _run(_skill(), _tension(ent.id), tmp_db)
    apply_effects(tmp_db, effects)

    # Every claim (all 250, not just the first 200) is now processed → the entity
    # drops off the board entirely rather than re-firing on its unread tail.
    assert ent.id not in dict(unsynthesized_claim_counts_by_entity(tmp_db, field_id=_FIELD))


# ── never writes; gateway lands the intent ───────────────────────────────────


def test_skill_emits_intent_without_writing(tmp_db: MeshConnection) -> None:
    ent = _entity(tmp_db, "WriteNet")
    src = _source(tmp_db, "s7")
    _claim(tmp_db, ent.id, src.id, "has_capability", {"capability": "speech"})

    assert list_beliefs(tmp_db, field_id=_FIELD) == []

    effects = _run(_skill(), _tension(ent.id), tmp_db)
    assert effects
    # run() touched no rows: still no beliefs until the gateway applies the effects.
    assert list_beliefs(tmp_db, field_id=_FIELD) == []

    report = apply_effects(tmp_db, effects)
    assert report.beliefs_created == 1
    held = list_beliefs(tmp_db, field_id=_FIELD)
    assert len(held) == 1
    assert held[0].topic == capability_topic(ent.id)


def test_no_claims_no_effects(tmp_db: MeshConnection) -> None:
    ent = _entity(tmp_db, "EmptyNet")
    assert _run(_skill(), _tension(ent.id), tmp_db) == []


def test_full_slice_create_then_revise_through_gateway(
    tmp_db: MeshConnection,
) -> None:
    """Two rounds on the (deterministic, name-keyed) capability belief: round one
    creates it, a new capability claim in round two revises it — all via effects
    through the real gateway, with append-only history preserved."""
    ent = _entity(tmp_db, "RecordNet")
    src = _source(tmp_db, "s8")
    _claim(tmp_db, ent.id, src.id, "has_capability", {"capability": "vision"})
    apply_effects(tmp_db, _run(_skill(), _tension(ent.id), tmp_db))
    belief = next(
        b for b in list_beliefs(tmp_db, field_id=_FIELD)
        if b.topic == capability_topic(ent.id)
    )

    _claim(tmp_db, ent.id, src.id, "has_capability", {"capability": "planning"})
    effects = _run(_skill(), _tension(ent.id), tmp_db)
    assert any(isinstance(e, ReviseBeliefEffect) for e in effects)
    apply_effects(tmp_db, effects)

    revs = list_revisions(tmp_db, belief_id=belief.id)
    assert len(revs) == 1
    assert "planning" in revs[0].new_statement


def test_relational_edge_lands_through_gateway(tmp_db: MeshConnection) -> None:
    a = _entity(tmp_db, "EdgeA")
    b = _entity(tmp_db, "EdgeB")
    src = _source(tmp_db, "s9")
    claim = _claim(tmp_db, a.id, src.id, "outperforms", {"compared_to": "EdgeB"})

    apply_effects(tmp_db, _run(_skill(), _tension(a.id), tmp_db))

    edge = find_relationship(tmp_db, a.id, b.id, "outperforms")
    assert edge is not None
    assert claim.id in edge.evidence_claim_ids
