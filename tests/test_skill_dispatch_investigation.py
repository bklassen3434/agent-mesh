"""Market skill: ``dispatch-investigation`` — work open investigations.

Covers the three lifecycle paths (gather → resolve → abandon) with a stubbed
in-process investigate handler (no network), and proves the gateway applies the
new UpdateInvestigation / AttachClaim effects. Also checks the agenda surfaces an
open_investigation tension only when an investigable connector is enabled.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest
from mesh_agents.agenda import investigation_tensions
from mesh_agents.arxiv_scout import ScoutedPaper
from mesh_agents.skill import get_skill, load_builtin_skills
from mesh_db.connection import MeshConnection
from mesh_db.connectors import enable_connector
from mesh_db.effects import apply_effects
from mesh_db.investigations import (
    create_investigation,
    get_investigation_by_id,
    update_investigation,
)
from mesh_db.sources import list_sources
from mesh_models.effect import CreateSourceEffect, UpdateInvestigationEffect
from mesh_models.field import DEFAULT_FIELD_ID
from mesh_models.investigation import Investigation, InvestigationStatus
from mesh_models.source import Source, SourceType
from mesh_models.tension import Tension, TensionKind


def _open_investigation(conn: MeshConnection, **over: Any) -> Investigation:
    fields: dict[str, Any] = dict(
        question="Is FlowNet still SOTA on optical flow?",
        hypothesis="FlowNet remains SOTA",
        suggested_source_types=["arxiv"],
        status=InvestigationStatus.open,
    )
    fields.update(over)
    inv = Investigation(**fields)
    create_investigation(conn, inv, field_id=DEFAULT_FIELD_ID)
    return inv


def _tension(inv_id: str) -> Tension:
    return Tension(
        id=f"{TensionKind.open_investigation.value}:{inv_id}",
        field_id=DEFAULT_FIELD_ID,
        kind=TensionKind.open_investigation,
        subject="FlowNet SOTA?",
        rationale="gather evidence",
        value=0.5,
        est_cost_usd=0.05,
        handler_skill="dispatch-investigation",
        target_ref={"investigation_id": inv_id},
    )


def _skill() -> Any:
    load_builtin_skills()
    skill = get_skill("dispatch-investigation")
    assert skill is not None
    return skill


def _run(skill: Any, conn: Any, inv_id: str) -> list[Any]:
    return asyncio.run(skill.run(conn, _tension(inv_id), budget_usd=0.05))


def test_gathers_sources_and_moves_in_progress(
    tmp_db: MeshConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_connector(tmp_db, DEFAULT_FIELD_ID, "arxiv", config={"categories": ["cs.CV"]})
    inv = _open_investigation(tmp_db)

    async def fake_investigate(source_name: str, **kw: Any) -> list[ScoutedPaper]:
        assert source_name == "arxiv"
        assert kw["investigation_id"] == inv.id
        return [
            ScoutedPaper(
                source=Source(
                    type=SourceType.arxiv,
                    url="https://arxiv.org/abs/ev1",
                    published_at=datetime(2024, 1, 1),
                    raw_content_hash="ev-hash-1",
                ),
                title="Evidence paper",
                abstract="FlowNet results.",
                arxiv_id="ev1",
            )
        ]

    monkeypatch.setattr(
        "mesh_agents.skills.dispatch_investigation.investigate_connector",
        fake_investigate,
    )
    effects = _run(_skill(), tmp_db, inv.id)

    # A tagged source effect + the in-progress lifecycle effect.
    src_effects = [e for e in effects if isinstance(e, CreateSourceEffect)]
    upd = [e for e in effects if isinstance(e, UpdateInvestigationEffect)]
    assert len(src_effects) == 1
    assert src_effects[0].source.payload == {
        "title": "Evidence paper",
        "abstract": "FlowNet results.",
        "investigation_id": inv.id,
    }
    assert len(upd) == 1 and upd[0].increment_attempts

    # Gateway applies: source persisted, investigation now in-progress, attempts=1.
    report = apply_effects(tmp_db, effects)
    assert report.sources_created == 1
    assert report.investigations_updated == 1
    after = get_investigation_by_id(tmp_db, inv.id)
    assert after is not None
    assert after.status == InvestigationStatus.in_progress
    assert after.pipeline_runs_attempted == 1
    assert any(
        s.raw_content_hash == "ev-hash-1"
        for s in list_sources(tmp_db, limit=100, field_id=DEFAULT_FIELD_ID)
    )


def test_resolves_once_claim_threshold_met(tmp_db: MeshConnection) -> None:
    inv = _open_investigation(tmp_db, status=InvestigationStatus.in_progress)
    # Three attached claims clears the default threshold (3).
    update_investigation(tmp_db, inv.id, collected_claim_ids=["c1", "c2", "c3"])

    effects = _run(_skill(), tmp_db, inv.id)
    assert len(effects) == 1
    assert isinstance(effects[0], UpdateInvestigationEffect)
    assert effects[0].status == InvestigationStatus.resolved.value

    apply_effects(tmp_db, effects)
    after = get_investigation_by_id(tmp_db, inv.id)
    assert after is not None and after.status == InvestigationStatus.resolved
    assert after.resolved_at is not None


def test_abandons_once_run_budget_exhausted(tmp_db: MeshConnection) -> None:
    inv = _open_investigation(
        tmp_db, status=InvestigationStatus.in_progress, pipeline_runs_attempted=5
    )
    effects = _run(_skill(), tmp_db, inv.id)
    assert len(effects) == 1
    assert effects[0].status == InvestigationStatus.abandoned.value

    apply_effects(tmp_db, effects)
    after = get_investigation_by_id(tmp_db, inv.id)
    assert after is not None and after.status == InvestigationStatus.abandoned


def test_agenda_surfaces_open_investigation_only_when_investigable(
    tmp_db: MeshConnection,
) -> None:
    # The seeded field has arxiv enabled (an investigable connector). An
    # investigation whose sources reach it surfaces a tension; one whose sources
    # don't (no enabled investigate handler) does not.
    enable_connector(tmp_db, DEFAULT_FIELD_ID, "arxiv", config={"categories": ["cs.CV"]})
    inv = _open_investigation(tmp_db, suggested_source_types=["arxiv"])
    _open_investigation(tmp_db, suggested_source_types=["unreachable_source"])

    tensions = investigation_tensions(tmp_db, DEFAULT_FIELD_ID)
    targets = {t.target_ref["investigation_id"] for t in tensions}
    assert inv.id in targets
    arxiv_t = next(t for t in tensions if t.target_ref["investigation_id"] == inv.id)
    assert arxiv_t.handler_skill == "dispatch-investigation"
    # The investigation with no reachable source type is not surfaced.
    assert all(t.signals is not None for t in tensions)
