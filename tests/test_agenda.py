"""Phase 0 of the agentic migration: the self-writing to-do list (the Agenda).

Exercised against the seeded test container. ``compute_agenda`` is read-only and
LLM-free, so no mocks/keys are needed — we seed a board, compute the agenda, and
assert the right tensions appear, rank by value-per-dollar, and clear a budget.
"""
from __future__ import annotations

from datetime import UTC, datetime

from mesh_agents.agenda import compute_agenda
from mesh_db.beliefs import create_belief
from mesh_db.claims import create_claim
from mesh_db.connection import MeshConnection
from mesh_db.entities import create_entity
from mesh_db.sources import create_source
from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.entity import Entity, EntityType
from mesh_models.source import Source, SourceType
from mesh_models.tension import TensionKind

_NOW = datetime(2026, 6, 13, tzinfo=UTC)
_FIELD = "ai-robotics"


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


def test_unread_source_becomes_a_tension(tmp_db: MeshConnection) -> None:
    _source(tmp_db, "unread-paper")
    agenda = compute_agenda(tmp_db, _FIELD, field_slug=_FIELD)

    unread = [t for t in agenda.tensions if t.kind == TensionKind.unextracted_source]
    assert len(unread) == 1
    assert unread[0].handler_skill == "extract-source"
    assert "example.com/unread-paper" in unread[0].subject


def test_read_source_is_not_a_tension(tmp_db: MeshConnection) -> None:
    ent = create_entity(tmp_db, Entity(canonical_name="ReadNet", type=EntityType.model))
    src = _source(tmp_db, "read-paper")
    create_claim(
        tmp_db,
        Claim(
            predicate="has_capability",
            subject_entity_id=ent.id,
            object={"capability": "reasoning"},
            source_id=src.id,
            extracted_at=_NOW,
            extracted_by_agent="claim_extractor",
            raw_excerpt="…",
        ),
    )
    agenda = compute_agenda(tmp_db, _FIELD, field_slug=_FIELD)
    # The source has a claim → not an unread-source tension.
    assert not any(
        t.kind == TensionKind.unextracted_source and t.target_ref.get("source_id") == src.id
        for t in agenda.tensions
    )


def test_agenda_is_ranked_by_value_per_dollar(tmp_db: MeshConnection) -> None:
    # A mix: an unread source (cheap) + a thin belief (pricier investigation).
    ent = create_entity(tmp_db, Entity(canonical_name="ThinNet", type=EntityType.model))
    src = _source(tmp_db, "thin-evidence")
    claim = create_claim(
        tmp_db,
        Claim(
            predicate="has_capability",
            subject_entity_id=ent.id,
            object={"capability": "planning"},
            source_id=src.id,
            extracted_at=_NOW,
            extracted_by_agent="claim_extractor",
            raw_excerpt="…",
        ),
    )
    create_belief(
        tmp_db,
        Belief(
            topic="thinnet-capability",
            statement="ThinNet can plan over long horizons",
            supporting_claim_ids=[claim.id],
            is_currently_held=True,
        ),
    )
    _source(tmp_db, "fresh-unread")  # a second, genuinely unread source

    agenda = compute_agenda(tmp_db, _FIELD, field_slug=_FIELD)
    scores = [t.score for t in agenda.tensions]
    assert scores == sorted(scores, reverse=True)  # ranked, descending
    # Cheap unread source should out-rank the costly investigation tension.
    assert agenda.tensions[0].kind == TensionKind.unextracted_source


def test_budget_funds_top_down_and_defers_the_rest(tmp_db: MeshConnection) -> None:
    for i in range(5):
        _source(tmp_db, f"paper-{i}")
    # Tiny budget: only some of the ~0.008-each unread sources fit.
    agenda = compute_agenda(tmp_db, _FIELD, field_slug=_FIELD, budget_usd=0.02)
    assert agenda.funded_count >= 1
    assert agenda.funded_count < agenda.total
    assert agenda.funded_cost_usd <= 0.02 + 1e-9
    # Funded ids are a prefix of the ranked list (greedy top-down).
    funded = set(agenda.funded_ids)
    ranks = [i for i, t in enumerate(agenda.tensions) if t.id in funded]
    assert ranks == list(range(len(ranks)))


def test_empty_field_is_quiescent(tmp_db: MeshConnection) -> None:
    agenda = compute_agenda(tmp_db, "does-not-exist", field_slug="does-not-exist")
    assert agenda.tensions == []
    assert agenda.quiescent is True
