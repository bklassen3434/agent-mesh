"""``maintain-belief`` skill — belief aging as controller effects (LLM-free).

The skill plans the same decay/archival decisions as the standalone sweep but
emits them as append-only ``ReviseBeliefEffect``s through the write gateway. These
tests pin the two behaviours that matter for correctness: decay lowers confidence
*verbatim* (the maintenance revision opts out of evidence-derived recomputation,
so an injected ``confidence_fn`` must not clobber it), and archival flips the
belief out of the held set without deleting any row.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from mesh_agents.skills.maintain_belief import MaintainBeliefSkill
from mesh_db.beliefs import create_belief, get_belief_by_id, list_beliefs
from mesh_db.claims import create_claim
from mesh_db.connection import MeshConnection
from mesh_db.effects import apply_effects
from mesh_db.entities import create_entity
from mesh_db.revisions import list_revisions
from mesh_db.sources import create_source
from mesh_models.belief import Belief
from mesh_models.claim import Claim, ClaimStatus
from mesh_models.entity import Entity, EntityType
from mesh_models.field import DEFAULT_FIELD_ID
from mesh_models.source import Source, SourceType
from mesh_models.tension import Tension, TensionKind


def _aging_tension() -> Tension:
    return Tension(
        id=f"aging_belief:{DEFAULT_FIELD_ID}",
        field_id=DEFAULT_FIELD_ID,
        kind=TensionKind.aging_belief,
        subject="belief maintenance",
        rationale="aging",
        value=0.2,
        est_cost_usd=0.001,
        handler_skill="maintain-belief",
        target_ref={"field_id": DEFAULT_FIELD_ID},
    )


def _live_claim(conn: MeshConnection) -> str:
    ent = create_entity(conn, Entity(canonical_name="M", type=EntityType.model))
    src = create_source(
        conn,
        Source(
            type=SourceType.arxiv, url="https://arxiv.org/abs/maintain",
            published_at=datetime.now(UTC), raw_content_hash="hm",
        ),
    )
    claim = Claim(
        predicate="achieves_score", subject_entity_id=ent.id, object={"score": 9.0},
        source_id=src.id, extracted_by_agent="x", raw_excerpt="e",
        confidence=0.7, status=ClaimStatus.active,
    )
    create_claim(conn, claim)
    return claim.id


def _belief(conn: MeshConnection, topic: str, *, supporting: list[str], age_days: int) -> str:
    b = Belief(
        topic=topic,
        statement=f"statement for {topic}",
        supporting_claim_ids=supporting,
        confidence=0.8,
        last_revised_at=datetime.now(UTC) - timedelta(days=age_days),
    )
    create_belief(conn, b)
    return b.id


def test_maintain_belief_decays_verbatim_and_archives(tmp_db: MeshConnection) -> None:
    cid = _live_claim(tmp_db)
    decaying = _belief(tmp_db, "sota:decay", supporting=[cid], age_days=200)  # > halflife
    dead = _belief(tmp_db, "sota:dead", supporting=[], age_days=400)  # > archive, unsupported

    effects = asyncio.run(
        MaintainBeliefSkill().run(tmp_db, _aging_tension(), budget_usd=0.0)
    )
    assert len(effects) == 2
    assert {e.set_not_held for e in effects} == {True, False}
    assert all(e.recompute_confidence is False for e in effects)

    # Apply with a confidence_fn that would push confidence to 0.99 if consulted —
    # it must be ignored for these maintenance revisions.
    report = apply_effects(tmp_db, effects, confidence_fn=lambda _c, _b: 0.99)
    assert report.beliefs_revised == 2
    assert not report.errors

    d = get_belief_by_id(tmp_db, decaying)
    assert d is not None
    assert d.is_currently_held is True
    assert d.confidence < 0.8 and d.confidence != 0.99  # decayed verbatim, not recomputed

    a = get_belief_by_id(tmp_db, dead)
    assert a is not None
    assert a.is_currently_held is False  # archived out of the held set
    # Append-only: both rows still exist, each gained a revision.
    assert len(list_revisions(tmp_db, decaying)) >= 1
    assert len(list_revisions(tmp_db, dead)) >= 1


def test_maintain_belief_noop_on_fresh_corpus(tmp_db: MeshConnection) -> None:
    _belief(tmp_db, "sota:fresh", supporting=[], age_days=1)
    effects = asyncio.run(
        MaintainBeliefSkill().run(tmp_db, _aging_tension(), budget_usd=0.0)
    )
    assert effects == []
    assert len(list_beliefs(tmp_db, currently_held=True)) == 1
