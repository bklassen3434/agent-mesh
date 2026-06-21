"""Phase 0 of the agentic migration: the self-writing to-do list (the Agenda).

Exercised against the seeded test container. ``compute_agenda`` is read-only and
LLM-free, so no mocks/keys are needed — we seed a board, compute the agenda, and
assert the right tensions appear, rank by value-per-dollar, and clear a budget.
"""
from __future__ import annotations

from datetime import UTC, datetime

from mesh_agents.agenda import _KIND_TIER, compute_agenda, resolve_tier
from mesh_db.beliefs import create_belief
from mesh_db.claims import create_claim
from mesh_db.connection import MeshConnection
from mesh_db.entities import create_entity, set_entity_embedding
from mesh_db.investigations import create_investigation
from mesh_db.sources import create_source
from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.entity import Entity, EntityType
from mesh_models.investigation import Investigation, InvestigationOrigin, InvestigationStatus
from mesh_models.source import Source, SourceType
from mesh_models.tension import ReasoningTier, TensionKind

_NOW = datetime(2026, 6, 13, tzinfo=UTC)
_FIELD = "ai-robotics"


# ── reasoning tiers (pure — no DB) ───────────────────────────────────────────


def test_every_kind_has_a_default_tier() -> None:
    assert set(_KIND_TIER) == set(TensionKind)
    # The deep family — the cases that need evidence the board doesn't have yet.
    assert _KIND_TIER[TensionKind.under_evidenced_entity] is ReasoningTier.deep
    assert _KIND_TIER[TensionKind.thin_belief] is ReasoningTier.deep
    assert _KIND_TIER[TensionKind.open_investigation] is ReasoningTier.deep
    assert _KIND_TIER[TensionKind.contradicted_belief] is ReasoningTier.deep
    # Swarm: one answer, noisy path.
    assert _KIND_TIER[TensionKind.contested_claim] is ReasoningTier.swarm
    # Simple: the answer is already on the board.
    assert _KIND_TIER[TensionKind.unextracted_source] is ReasoningTier.simple


def test_gray_band_similarity_upgrades_a_merge_to_swarm() -> None:
    # Below auto-merge (0.93) but above auto-reject (0.80) → ambiguous → swarm.
    assert resolve_tier(TensionKind.merge_candidate, {"similarity": 0.85}) is ReasoningTier.swarm
    # At/above the high band → deterministic auto-merge → stays simple.
    assert resolve_tier(TensionKind.merge_candidate, {"similarity": 0.95}) is ReasoningTier.simple
    # Beliefs use their own (tighter) bands.
    assert resolve_tier(TensionKind.redundant_beliefs, {"similarity": 0.90}) is ReasoningTier.swarm
    assert resolve_tier(TensionKind.redundant_beliefs, {"similarity": 0.97}) is ReasoningTier.simple


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


# ── Phase 2a: the three new tension kinds ────────────────────────────────────


def test_lookalike_entities_become_a_merge_candidate(tmp_db: MeshConnection) -> None:
    a = create_entity(tmp_db, Entity(canonical_name="GPT-4", type=EntityType.model))
    b = create_entity(tmp_db, Entity(canonical_name="GPT 4", type=EntityType.model))
    # Identical embeddings → cosine similarity 1.0, well above the low band.
    vec = [1.0] + [0.0] * 383
    set_entity_embedding(tmp_db, a.id, vec)
    set_entity_embedding(tmp_db, b.id, vec)

    agenda = compute_agenda(tmp_db, _FIELD, field_slug=_FIELD)
    merges = [t for t in agenda.tensions if t.kind == TensionKind.merge_candidate]
    assert len(merges) == 1
    assert merges[0].handler_skill == "merge-candidate"
    assert {merges[0].target_ref["entity_id"], merges[0].target_ref["candidate_id"]} == {a.id, b.id}


def test_distinct_entities_are_not_merge_candidates(tmp_db: MeshConnection) -> None:
    a = create_entity(tmp_db, Entity(canonical_name="Alpha", type=EntityType.model))
    b = create_entity(tmp_db, Entity(canonical_name="Beta", type=EntityType.model))
    set_entity_embedding(tmp_db, a.id, [1.0] + [0.0] * 383)
    set_entity_embedding(tmp_db, b.id, [0.0, 1.0] + [0.0] * 382)  # orthogonal → sim 0
    agenda = compute_agenda(tmp_db, _FIELD, field_slug=_FIELD)
    assert not any(t.kind == TensionKind.merge_candidate for t in agenda.tensions)


def test_challenged_belief_becomes_a_contested_claim(tmp_db: MeshConnection) -> None:
    ent = create_entity(tmp_db, Entity(canonical_name="ContestedNet", type=EntityType.model))
    src = _source(tmp_db, "contested")
    support = create_claim(
        tmp_db,
        Claim(
            predicate="has_capability",
            subject_entity_id=ent.id,
            object={"capability": "x"},
            source_id=src.id,
            extracted_at=_NOW,
            extracted_by_agent="claim_extractor",
            raw_excerpt="…",
        ),
    )
    against = create_claim(
        tmp_db,
        Claim(
            predicate="critiques",
            subject_entity_id=ent.id,
            object={"note": "fails on edge cases"},
            source_id=src.id,
            extracted_at=_NOW,
            extracted_by_agent="skeptic",
            raw_excerpt="…",
        ),
    )
    create_belief(
        tmp_db,
        Belief(
            topic="contestednet-cap",
            statement="ContestedNet is great",
            supporting_claim_ids=[support.id],
            contradicting_claim_ids=[against.id],
            is_currently_held=True,
        ),
    )
    agenda = compute_agenda(tmp_db, _FIELD, field_slug=_FIELD)
    contested = [t for t in agenda.tensions if t.kind == TensionKind.contested_claim]
    assert len(contested) == 1
    assert contested[0].handler_skill == "challenge-belief"


def test_unreferenced_claim_becomes_unsynthesized(tmp_db: MeshConnection) -> None:
    ent = create_entity(tmp_db, Entity(canonical_name="LonelyClaimNet", type=EntityType.model))
    src = _source(tmp_db, "unsynth")
    create_claim(
        tmp_db,
        Claim(
            predicate="has_capability",
            subject_entity_id=ent.id,
            object={"capability": "y"},
            source_id=src.id,
            extracted_at=_NOW,
            extracted_by_agent="claim_extractor",
            raw_excerpt="…",
        ),
    )
    agenda = compute_agenda(tmp_db, _FIELD, field_slug=_FIELD)
    unsynth = [
        t
        for t in agenda.tensions
        if t.kind == TensionKind.unsynthesized_claims
        and t.target_ref.get("entity_id") == ent.id
    ]
    assert len(unsynth) == 1
    assert unsynth[0].handler_skill == "synthesize-belief"


def test_claim_in_a_belief_is_not_unsynthesized(tmp_db: MeshConnection) -> None:
    ent = create_entity(tmp_db, Entity(canonical_name="SynthedNet", type=EntityType.model))
    src = _source(tmp_db, "synthed")
    claim = create_claim(
        tmp_db,
        Claim(
            predicate="has_capability",
            subject_entity_id=ent.id,
            object={"capability": "z"},
            source_id=src.id,
            extracted_at=_NOW,
            extracted_by_agent="claim_extractor",
            raw_excerpt="…",
        ),
    )
    create_belief(
        tmp_db,
        Belief(
            topic="synthednet-cap",
            statement="SynthedNet does z",
            supporting_claim_ids=[claim.id],
            is_currently_held=True,
        ),
    )
    agenda = compute_agenda(tmp_db, _FIELD, field_slug=_FIELD)
    assert not any(
        t.kind == TensionKind.unsynthesized_claims
        and t.target_ref.get("entity_id") == ent.id
        for t in agenda.tensions
    )


# ── deep adjudication: contradicted load-bearing beliefs ─────────────────────


def _seed_contradicted(conn: MeshConnection, *, confidence: float, supports: int) -> str:
    """A held belief with ``supports`` supporting claims, ``confidence``, and one
    fresh contradicting claim. Returns the belief id."""
    ent = create_entity(conn, Entity(canonical_name="LoadBearer", type=EntityType.model))
    src = _source(conn, f"lb-{confidence}-{supports}")
    support_ids = []
    for i in range(supports):
        c = create_claim(
            conn,
            Claim(
                predicate="achieves_score",
                subject_entity_id=ent.id,
                object={"score": 90 - i, "benchmark": "MMLU"},
                source_id=src.id,
                extracted_at=_NOW,
                extracted_by_agent="claim_extractor",
                raw_excerpt="…",
            ),
        )
        support_ids.append(c.id)
    against = create_claim(
        conn,
        Claim(
            predicate="critiques",
            subject_entity_id=ent.id,
            object={"note": "fails to reproduce"},
            source_id=src.id,
            extracted_at=_NOW,
            extracted_by_agent="skeptic",
            raw_excerpt="…",
        ),
    )
    belief = create_belief(
        conn,
        Belief(
            topic="loadbearer-sota",
            statement="LoadBearer is SOTA on MMLU",
            supporting_claim_ids=support_ids,
            contradicting_claim_ids=[against.id],
            confidence=confidence,
            is_currently_held=True,
        ),
    )
    return belief.id


def test_contradicted_load_bearing_belief_becomes_a_deep_tension(tmp_db: MeshConnection) -> None:
    belief_id = _seed_contradicted(tmp_db, confidence=0.85, supports=2)
    agenda = compute_agenda(tmp_db, _FIELD, field_slug=_FIELD)

    deep = [t for t in agenda.tensions if t.kind == TensionKind.contradicted_belief]
    assert len(deep) == 1
    assert deep[0].handler_skill == "adjudicate-contradiction"
    assert deep[0].tier is ReasoningTier.deep
    assert deep[0].target_ref["belief_id"] == belief_id
    # ...and it pre-empts the routine swarm-tier challenge for the same belief.
    contested = [
        t
        for t in agenda.tensions
        if t.kind == TensionKind.contested_claim
        and t.target_ref.get("belief_id") == belief_id
    ]
    assert contested == []


def test_low_confidence_or_thin_belief_stays_a_contested_claim(tmp_db: MeshConnection) -> None:
    # Confident but not load-bearing (1 supporting claim) → not deep-adjudicated.
    thin = _seed_contradicted(tmp_db, confidence=0.85, supports=1)
    agenda = compute_agenda(tmp_db, _FIELD, field_slug=_FIELD)
    assert not any(t.kind == TensionKind.contradicted_belief for t in agenda.tensions)
    assert any(
        t.kind == TensionKind.contested_claim and t.target_ref.get("belief_id") == thin
        for t in agenda.tensions
    )


def test_adjudication_in_flight_suppresses_the_contradicted_tension(tmp_db: MeshConnection) -> None:
    belief_id = _seed_contradicted(tmp_db, confidence=0.85, supports=2)
    # A gather investigation is already open for this belief → withhold the tension.
    create_investigation(
        tmp_db,
        Investigation(
            question="gather",
            opened_by_belief_id=belief_id,
            origin=InvestigationOrigin.adjudication,
            status=InvestigationStatus.in_progress,
        ),
        field_id=_FIELD,
    )
    agenda = compute_agenda(tmp_db, _FIELD, field_slug=_FIELD)
    assert not any(t.kind == TensionKind.contradicted_belief for t in agenda.tensions)
