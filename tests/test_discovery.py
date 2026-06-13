"""Phase 22c: gap/trend analyzer + hypothesis drafter.

``analyze_field`` is exercised against a seeded corpus on the test container;
``draft_hypotheses`` and ``build_discovery_investigations`` use a mock LLMClient
so no network/keys are needed.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mesh_agents.discovery import (
    DiscoveryProposal,
    DiscoveryProposals,
    GapKind,
    GapSignal,
    analyze_field,
    build_discovery_investigations,
    draft_hypotheses,
)
from mesh_db.beliefs import create_belief
from mesh_db.claims import create_claim
from mesh_db.connection import MeshConnection
from mesh_db.entities import create_entity
from mesh_db.investigations import create_investigation
from mesh_db.sources import create_source
from mesh_llm import LLMResponseError
from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.entity import Entity, EntityType
from mesh_models.field import AI_ROBOTICS_PROFILE
from mesh_models.investigation import Investigation, InvestigationOrigin
from mesh_models.source import Source, SourceType

_NOW = datetime(2026, 6, 13, tzinfo=UTC)


class _MockLLM:
    """Minimal LLMClient stand-in: returns a fixed DiscoveryProposals."""

    model = "mock-model"

    def __init__(self, result: Any) -> None:
        self._result = result

    def health_check(self) -> None:  # pragma: no cover - unused
        return None

    def complete_with_latency(self, *a: Any, **kw: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def complete_with_usage(self, *a: Any, **kw: Any) -> Any:
        if isinstance(self._result, Exception):
            raise self._result
        from mesh_llm.usage import LLMUsage

        return self._result, 10, LLMUsage(input_tokens=1, output_tokens=1)


def _source(conn: MeshConnection, type_: SourceType = SourceType.arxiv) -> Source:
    src = Source(
        type=type_,
        url=f"https://example.com/{type_.value}/{datetime.now(UTC).timestamp()}",
        published_at=_NOW,
        raw_content_hash=f"h{datetime.now(UTC).timestamp()}",
    )
    return create_source(conn, src)


def _claim(conn: MeshConnection, entity_id: str, source_id: str) -> Claim:
    c = Claim(
        predicate="has_capability",
        subject_entity_id=entity_id,
        object={"capability": "reasoning"},
        source_id=source_id,
        extracted_at=_NOW,
        extracted_by_agent="claim_extractor",
        raw_excerpt="…",
    )
    return create_claim(conn, c)


def test_analyze_flags_under_evidenced_entity_not_well_evidenced(
    tmp_db: MeshConnection,
) -> None:
    thin = create_entity(tmp_db, Entity(canonical_name="ObscureNet", type=EntityType.model))
    rich = create_entity(tmp_db, Entity(canonical_name="PopularNet", type=EntityType.model))
    for _ in range(3):
        src = _source(tmp_db)
        _claim(tmp_db, rich.id, src.id)

    gaps = analyze_field(tmp_db, "ai-robotics", rising_min_claims=3)
    by_kind = {g.kind: g for g in gaps}

    under = [g for g in gaps if g.kind == GapKind.under_evidenced_entity]
    assert any(g.entity_id == thin.id for g in under)
    assert all(g.entity_id != rich.id for g in under)

    # The busy entity surfaces as a rising trend instead.
    assert GapKind.rising_topic in by_kind
    assert any(
        g.entity_id == rich.id and g.kind == GapKind.rising_topic for g in gaps
    )


def test_analyze_flags_thin_belief(tmp_db: MeshConnection) -> None:
    ent = create_entity(tmp_db, Entity(canonical_name="ThinNet", type=EntityType.model))
    src = _source(tmp_db)
    claim = _claim(tmp_db, ent.id, src.id)
    create_belief(
        tmp_db,
        Belief(
            topic="thinnet-capability",
            statement="ThinNet can do long-horizon planning",
            supporting_claim_ids=[claim.id],
            is_currently_held=True,
        ),
    )
    gaps = analyze_field(tmp_db, "ai-robotics")
    assert any(g.kind == GapKind.thin_belief for g in gaps)


def test_analyze_is_field_scoped(tmp_db: MeshConnection) -> None:
    # An entity in the seeded field is visible; a query for an unknown field is empty.
    create_entity(tmp_db, Entity(canonical_name="ScopedNet", type=EntityType.model))
    assert analyze_field(tmp_db, "ai-robotics")  # non-empty
    assert analyze_field(tmp_db, "does-not-exist") == []


def _gap(gap_id: str, **kw: Any) -> GapSignal:
    base: dict[str, Any] = {
        "gap_id": gap_id,
        "kind": GapKind.under_evidenced_entity,
        "subject": "X",
        "rationale": "thin",
        "entity_id": "e1",
    }
    base.update(kw)
    return GapSignal(**base)


def test_draft_filters_unknown_gap_and_disallowed_source() -> None:
    gaps = [_gap("under_evidenced_entity:e1")]
    result = DiscoveryProposals(
        proposals=[
            DiscoveryProposal(
                gap_id="under_evidenced_entity:e1",
                hypothesis="Search arxiv for ObscureNet benchmarks",
                suggested_source_types=["arxiv", "tiktok"],  # tiktok not allowed
                rationale="closes the gap",
            ),
            DiscoveryProposal(
                gap_id="phantom:gap",  # unknown gap_id → dropped
                hypothesis="irrelevant",
                suggested_source_types=["arxiv"],
            ),
        ]
    )
    proposals, usage, model = draft_hypotheses(
        AI_ROBOTICS_PROFILE,
        gaps,
        llm=_MockLLM(result),
        allowed_source_types=["arxiv", "github"],
    )
    assert len(proposals) == 1
    assert proposals[0].suggested_source_types == ["arxiv"]  # tiktok stripped
    assert model == "mock-model"
    assert usage is not None


def test_draft_degrades_on_llm_failure() -> None:
    gaps = [_gap("under_evidenced_entity:e1")]
    proposals, usage, model = draft_hypotheses(
        AI_ROBOTICS_PROFILE,
        gaps,
        llm=_MockLLM(LLMResponseError("bad json")),
        allowed_source_types=["arxiv"],
    )
    assert proposals == []
    assert usage is None
    assert model == ""


def test_draft_noop_without_allowed_sources() -> None:
    proposals, _, _ = draft_hypotheses(
        AI_ROBOTICS_PROFILE,
        [_gap("under_evidenced_entity:e1")],
        llm=_MockLLM(DiscoveryProposals()),
        allowed_source_types=[],
    )
    assert proposals == []


def test_build_investigations_dedupes_against_existing() -> None:
    gaps = [
        _gap("under_evidenced_entity:e1", entity_id="e1"),
        _gap("thin_belief:b1", kind=GapKind.thin_belief, entity_id=None, belief_id="b1"),
    ]
    proposals = [
        DiscoveryProposal(gap_id="under_evidenced_entity:e1", hypothesis="h1",
                          suggested_source_types=["arxiv"]),
        DiscoveryProposal(gap_id="thin_belief:b1", hypothesis="h2",
                          suggested_source_types=["arxiv"]),
    ]
    # An existing open investigation already covers entity e1.
    existing = [Investigation(question="q", target_entity_id="e1")]
    out = build_discovery_investigations(gaps, proposals, existing)
    assert len(out) == 1
    inv = out[0]
    assert inv.opened_by_belief_id == "b1"
    assert inv.origin == InvestigationOrigin.discovery
    assert inv.trigger_rationale


def test_build_investigations_dedupes_within_batch() -> None:
    gaps = [_gap("under_evidenced_entity:e1", entity_id="e1")]
    proposals = [
        DiscoveryProposal(gap_id="under_evidenced_entity:e1", hypothesis="h1",
                          suggested_source_types=["arxiv"]),
        DiscoveryProposal(gap_id="under_evidenced_entity:e1", hypothesis="h1-dup",
                          suggested_source_types=["github"]),
    ]
    out = build_discovery_investigations(gaps, proposals, [])
    assert len(out) == 1


def test_persisted_discovery_investigation_roundtrips(tmp_db: MeshConnection) -> None:
    gaps = [_gap("under_evidenced_entity:e1", entity_id=None)]
    proposals = [
        DiscoveryProposal(gap_id="under_evidenced_entity:e1", hypothesis="search it",
                          suggested_source_types=["arxiv"], rationale="why")
    ]
    built = build_discovery_investigations(gaps, proposals, [])
    saved = create_investigation(tmp_db, built[0])
    from mesh_db.investigations import get_investigation_by_id

    fetched = get_investigation_by_id(tmp_db, saved.id)
    assert fetched is not None
    assert fetched.origin == InvestigationOrigin.discovery
    assert fetched.trigger_rationale is not None
