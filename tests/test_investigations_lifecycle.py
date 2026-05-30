"""Phase 7a.6 lifecycle tests.

Cover the four state transitions the coordinator drives plus the
Curator suggestion → orchestrator persistence handoff. These are pure
DAL + small functions; the end-to-end coordinator dispatch is covered
by the existing pipeline tests + manual smoke runs.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mesh_agents.curator import (
    BeliefForCuration,
    CuratorInput,
    InvestigationSuggestion,
    select_beliefs_to_challenge_pure,
)
from mesh_db.connection import MeshConnection
from mesh_db.investigations import (
    attach_claim_to_investigation,
    create_investigation,
    get_investigation_by_id,
    list_investigations,
    update_investigation,
)
from mesh_models.investigation import Investigation, InvestigationStatus

_NOW = datetime(2026, 5, 27, tzinfo=UTC)


def _investigation() -> Investigation:
    return Investigation(
        question="Is X still SOTA?",
        hypothesis="Is X still SOTA on MMLU?",
        target_entity_id="entity-1",
        suggested_source_types=["arxiv", "leaderboard"],
        opened_by_belief_id="belief-1",
    )


def test_attach_claim_appends_no_duplicates(tmp_db: MeshConnection) -> None:
    inv = create_investigation(tmp_db, _investigation())
    inv = attach_claim_to_investigation(tmp_db, inv.id, "claim-a")
    inv = attach_claim_to_investigation(tmp_db, inv.id, "claim-b")
    # Re-attaching the same id is a no-op.
    inv = attach_claim_to_investigation(tmp_db, inv.id, "claim-a")
    assert inv.collected_claim_ids == ["claim-a", "claim-b"]


def test_lifecycle_resolve_after_threshold(tmp_db: MeshConnection) -> None:
    inv = create_investigation(tmp_db, _investigation())
    for cid in ("c1", "c2", "c3"):
        attach_claim_to_investigation(tmp_db, inv.id, cid)
    update_investigation(
        tmp_db,
        inv.id,
        status=InvestigationStatus.resolved,
        resolved_at=_NOW,
    )
    fetched = get_investigation_by_id(tmp_db, inv.id)
    assert fetched is not None
    assert fetched.status == InvestigationStatus.resolved
    assert fetched.resolved_at is not None
    assert len(fetched.collected_claim_ids) == 3


def test_lifecycle_abandon_after_max_runs(tmp_db: MeshConnection) -> None:
    inv = create_investigation(tmp_db, _investigation())
    update_investigation(
        tmp_db,
        inv.id,
        status=InvestigationStatus.abandoned,
        pipeline_runs_attempted=5,
        resolved_at=_NOW,
    )
    fetched = get_investigation_by_id(tmp_db, inv.id)
    assert fetched is not None
    assert fetched.status == InvestigationStatus.abandoned
    assert fetched.pipeline_runs_attempted == 5


def test_list_filtered_by_status(tmp_db: MeshConnection) -> None:
    a = create_investigation(tmp_db, _investigation())
    b = create_investigation(tmp_db, _investigation())
    update_investigation(tmp_db, a.id, status=InvestigationStatus.resolved)
    open_ones = list_investigations(tmp_db, status=InvestigationStatus.open)
    assert {i.id for i in open_ones} == {b.id}


def _belief(
    belief_id: str,
    *,
    supporting: int = 5,
    revised_days_ago: int = 1,
    evidence_days_ago: int | None = 1,
    recent_contradiction: bool = False,
) -> BeliefForCuration:
    return BeliefForCuration(
        belief_id=belief_id,
        topic="sota:test",
        statement=f"belief {belief_id}",
        confidence=0.7,
        supporting_claim_count=supporting,
        contradicting_claim_count=0,
        last_revised_at=_NOW - timedelta(days=revised_days_ago),
        last_evidence_at=(
            _NOW - timedelta(days=evidence_days_ago)
            if evidence_days_ago is not None
            else None
        ),
        recent_contradicting_activity=recent_contradiction,
    )


def test_curator_emits_investigation_for_stale_evidence() -> None:
    beliefs = [
        _belief("fresh", supporting=5, evidence_days_ago=1),
        _belief("stale-evidence", supporting=5, evidence_days_ago=120),
    ]
    out = select_beliefs_to_challenge_pure(
        CuratorInput(beliefs=beliefs, now=_NOW, pick_count=5)
    )
    suggested_ids = {s.belief_id for s in out.investigation_suggestions}
    assert "stale-evidence" in suggested_ids
    assert "fresh" not in suggested_ids


def test_curator_emits_investigation_for_no_evidence() -> None:
    beliefs = [_belief("orphan", supporting=0, evidence_days_ago=None)]
    out = select_beliefs_to_challenge_pure(
        CuratorInput(beliefs=beliefs, now=_NOW, pick_count=5)
    )
    assert any(s.belief_id == "orphan" for s in out.investigation_suggestions)


def test_investigation_suggestion_carries_source_types() -> None:
    beliefs = [_belief("orphan", supporting=0, evidence_days_ago=None)]
    out = select_beliefs_to_challenge_pure(
        CuratorInput(beliefs=beliefs, now=_NOW, pick_count=5)
    )
    s: InvestigationSuggestion = out.investigation_suggestions[0]
    # Default suggested set is at least arxiv + leaderboard + blog.
    assert {"arxiv", "leaderboard", "blog"}.issubset(set(s.suggested_source_types))
